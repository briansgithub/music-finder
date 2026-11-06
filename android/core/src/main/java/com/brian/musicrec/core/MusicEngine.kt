package com.brian.musicrec.core

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import com.brian.musicrec.core.android.SqliteEngineStore
import com.brian.musicrec.core.engine.LocalEngine
import com.brian.musicrec.core.engine.NetFetcher
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit
import java.util.zip.GZIPInputStream

/** What the UI talks to: the on-device engine (default) or the optional remote PC engine. */
interface MusicEngine {
    val label: String
    /** null = nothing left after filters. */
    fun next(lane: String, enabledGenres: List<String>?): Recommendation?
    /** Returns how many actions can be undone afterwards (-1 = unknown). */
    fun rate(r: Recommendation, rating: String): Int
    fun notNow(r: Recommendation): Int
    /** Revert the latest rating/not-now; [current] is the card on screen now. null = nothing to undo. */
    fun undo(current: Recommendation?): Pair<Recommendation, Int>?
    fun dials(): DialState
    fun setDials(lane: String, preset: String, enabledGenres: List<String>?)
    fun exportJson(): String
    fun correctVideo(r: Recommendation, videoIdOrUrl: String): String
    /** If a background video lookup for [r] finishes in time, the card upgraded to an exact video; else null. */
    fun awaitVideo(r: Recommendation): Recommendation? = null
}

class RemoteMusicEngine(baseUrl: String) : MusicEngine {
    private val client = EngineClient(baseUrl)
    override val label = "remote $baseUrl"
    override fun next(lane: String, enabledGenres: List<String>?): Recommendation? = try {
        client.next(lane, enabledGenres)
    } catch (_: NoRecommendationException) {
        null
    }
    override fun rate(r: Recommendation, rating: String) = client.rate(r.artist, r.title, rating, r.lane, r.confidence, r.cluster)
    override fun notNow(r: Recommendation) = client.notNow(r.artist, r.title, r.lane)
    override fun undo(current: Recommendation?) = client.undo(current?.artist, current?.title)
    override fun dials() = client.dials()
    override fun setDials(lane: String, preset: String, enabledGenres: List<String>?) = client.setDials(lane, preset, enabledGenres)
    override fun exportJson(): String = client.exportRaw()
    override fun correctVideo(r: Recommendation, videoIdOrUrl: String): String = client.correctVideo(r.artist, r.title, videoIdOrUrl)
}

class OnDeviceMusicEngine(val engine: LocalEngine) : MusicEngine {
    override val label = "on-device"
    override fun awaitVideo(r: Recommendation): Recommendation? = engine.awaitVideo(r)
    override fun next(lane: String, enabledGenres: List<String>?) = engine.next(lane, enabledGenres)
    override fun rate(r: Recommendation, rating: String): Int {
        engine.rate(r.artist, r.title, rating, r.lane, r.cluster, r.confidence, shown = r)
        return engine.undoDepth()
    }
    override fun notNow(r: Recommendation): Int {
        engine.notNow(r.artist, r.title, r.lane, shown = r)
        return engine.undoDepth()
    }
    override fun undo(current: Recommendation?): Pair<Recommendation, Int>? =
        engine.undo(current?.let { com.brian.musicrec.core.engine.normalizeKey(it.artist, it.title) })?.let { it.rec to it.remaining }
    override fun dials() = engine.dials()
    override fun setDials(lane: String, preset: String, enabledGenres: List<String>?) {
        engine.setDials(lane, preset, enabledGenres)
    }
    override fun exportJson() = engine.exportJson()
    override fun correctVideo(r: Recommendation, videoIdOrUrl: String): String {
        val res = engine.correctVideo(r.artist, r.title, videoIdOrUrl)
        require(res.ok) { res.error.ifEmpty { "correction failed" } }
        return res.videoId
    }
}

/** Process-wide on-device engine (one SQLite DB, one session). */
object LocalEngineHolder {
    const val SEED_ASSET = "seed/musicfinder_seed.json.gz"
    @Volatile private var instance: LocalEngine? = null
    @Volatile var allowNetworkResolve: Boolean = true

    fun get(context: Context): LocalEngine = instance ?: synchronized(this) {
        instance ?: create(context.applicationContext).also { instance = it }
    }

    private fun create(ctx: Context): LocalEngine {
        val http = OkHttpClient.Builder().connectTimeout(5, TimeUnit.SECONDS).readTimeout(6, TimeUnit.SECONDS).build()
        val fetcher = NetFetcher { url, headers ->
            val req = Request.Builder().url(url).apply { headers.forEach { (k, v) -> header(k, v) } }.get().build()
            http.newCall(req).execute().use { res ->
                val body = res.body?.string().orEmpty()
                if (!res.isSuccessful) error("HTTP ${res.code}")
                body
            }
        }
        val engine = LocalEngine(
            store = SqliteEngineStore(ctx),
            net = fetcher,
            allowNetworkResolve = { allowNetworkResolve && isOnline(ctx) },
            log = { DebugLog.info(it) },
        )
        if (!engine.hasSeed()) {
            firstRun(ctx, engine)
        } else {
            mergeNewerBundledSeed(ctx, engine)
        }
        return engine
    }

    /**
     * App update shipped a newer seed bundle (e.g. with pre-resolved video IDs):
     * merge it in. Ratings, learned weights, corrections and history are kept
     * (ratings merge by key+time, weights insert-if-absent); only the candidate
     * pool is refreshed and new video IDs / no-video results are added.
     */
    fun mergeNewerBundledSeed(ctx: Context, engine: LocalEngine): Boolean {
        val bundled = bundledSeedExportedRaw(ctx) ?: return false
        if (engine.seedExportedRaw() == bundled) return false
        val text = readBundledSeed(ctx) ?: return false
        return try {
            val s = DebugLog.call("merge newer bundled seed ($bundled)") { engine.importText(text) }
            DebugLog.info("seed merge: $s")
            true
        } catch (e: Exception) {
            DebugLog.error("bundled seed merge failed", e)
            false
        }
    }

    /** `exported_at` from the head of the bundled seed (cheap: reads only the first few KB). */
    fun bundledSeedExportedRaw(ctx: Context): String? {
        for (name in listOf(SEED_ASSET, SEED_ASSET.removeSuffix(".gz"))) {
            try {
                ctx.assets.open(name).use { raw ->
                    val buf = java.io.BufferedInputStream(raw)
                    buf.mark(4)
                    val b0 = buf.read(); val b1 = buf.read()
                    buf.reset()
                    val stream = if (b0 == 0x1f && b1 == 0x8b) GZIPInputStream(buf) else buf
                    val head = ByteArray(4096)
                    var n = 0
                    while (n < head.size) { val r = stream.read(head, n, head.size - n); if (r < 0) break; n += r }
                    val text = String(head, 0, n, Charsets.UTF_8)
                    return Regex("\"exported_at\"\\s*:\\s*([0-9.eE+-]+)").find(text)?.groupValues?.get(1)
                }
            } catch (_: java.io.IOException) {
                continue
            }
        }
        return null
    }

    /** First run: import the bundled seed asset if present; otherwise the user imports a file. */
    fun firstRun(ctx: Context, engine: LocalEngine): Boolean {
        val text = readBundledSeed(ctx)
        if (text == null) {
            DebugLog.info("first run: no bundled seed asset ($SEED_ASSET) — import a seed or /export JSON in Settings")
            return false
        }
        return try {
            val s = DebugLog.call("import bundled seed") { engine.importText(text) }
            DebugLog.info("first run: $s")
            true
        } catch (e: Exception) {
            DebugLog.error("bundled seed import failed", e)
            false
        }
    }

    /**
     * The build tools store `*.json.gz` assets decompressed and without the `.gz`
     * suffix, so try both names and sniff the gzip magic.
     */
    fun readBundledSeed(ctx: Context): String? {
        for (name in listOf(SEED_ASSET, SEED_ASSET.removeSuffix(".gz"))) {
            try {
                val bytes = ctx.assets.open(name).use { it.readBytes() }
                val gz = bytes.size > 2 && bytes[0] == 0x1f.toByte() && bytes[1] == 0x8b.toByte()
                return if (gz) GZIPInputStream(bytes.inputStream()).bufferedReader().readText() else String(bytes, Charsets.UTF_8)
            } catch (_: java.io.IOException) {
                continue
            }
        }
        return null
    }

    fun isOnline(ctx: Context): Boolean = try {
        val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        caps?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED) == true
    } catch (_: Exception) {
        false
    }
}
