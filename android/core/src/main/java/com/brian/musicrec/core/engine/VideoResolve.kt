package com.brian.musicrec.core.engine

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/** Minimal blocking JSON GET used for optional online lookups (never for ranking). */
fun interface NetFetcher {
    /** Returns the body or throws (message should include the HTTP status). */
    fun get(url: String, headers: Map<String, String>): String
}

object YouTubeIds {
    private val VIDEO_ID_RE = Regex("^[A-Za-z0-9_-]{11}$")
    private val YOUTU_BE = Regex("youtu\\.be/([A-Za-z0-9_-]{11})")
    private val GENERIC = Regex("(?:v=|/shorts/|/embed/|/live/|/watch/|youtu\\.be/)([A-Za-z0-9_-]{11})")

    /** Port of `audio/youtube_url.extract_video_id`. */
    fun extract(urlOrId: String?): String? {
        val s = (urlOrId ?: "").trim()
        if (VIDEO_ID_RE.matches(s)) return s
        YOUTU_BE.find(s)?.let { return it.groupValues[1] }
        GENERIC.find(s)?.let { return it.groupValues[1] }
        return null
    }

    /** Python `urllib.parse.quote_plus` (UTF-8, safe = letters digits `_.-~`). */
    fun quotePlus(s: String): String {
        val sb = StringBuilder()
        for (b in s.toByteArray(Charsets.UTF_8)) {
            val c = b.toInt() and 0xff
            when {
                c == ' '.code -> sb.append('+')
                (c in 'a'.code..'z'.code) || (c in 'A'.code..'Z'.code) || (c in '0'.code..'9'.code) ||
                    c == '_'.code || c == '.'.code || c == '-'.code || c == '~'.code -> sb.append(c.toChar())
                else -> sb.append('%').append("0123456789ABCDEF"[c shr 4]).append("0123456789ABCDEF"[c and 15])
            }
        }
        return sb.toString()
    }

    fun watchUrl(videoId: String): String {
        val vid = extract(videoId) ?: videoId.trim()
        return if (vid.length == 11) "https://music.youtube.com/watch?v=$vid" else ""
    }

    fun searchUrl(artist: String, title: String): String =
        "https://music.youtube.com/search?q=" + quotePlus("$artist $title".trim())

    /** Port of `handoff.ytm_android_intent_uri`. */
    fun intentUri(artist: String, title: String, videoId: String = ""): String {
        var vid = if (videoId.isNotEmpty()) extract(videoId) ?: "" else ""
        if (vid.isEmpty() && videoId.trim().length == 11) vid = videoId.trim()
        val pkg = "#Intent;scheme=https;package=com.google.android.apps.youtube.music;S.browser_fallback_url="
        return if (vid.isNotEmpty()) {
            "intent://music.youtube.com/watch?v=$vid$pkg${quotePlus(watchUrl(vid))};end"
        } else {
            "intent://music.youtube.com/search?q=${quotePlus("$artist $title".trim())}$pkg${quotePlus(searchUrl(artist, title))};end"
        }
    }
}

data class ResolveResult(
    val ok: Boolean,
    val videoId: String,
    val source: String,
    val status: String,
    val chain: List<String> = emptyList(),
    val override: Boolean = false,
    val error: String = "",
)

/**
 * Port of `playresolve/pipeline.resolve_play_video_id`, reliability first:
 *  1. official/cached IDs (candidate field, prior auto-resolve cache, seed video IDs)
 *  2. MusicBrainz recording URL relations — network, only when nothing local,
 *     only when allowed, rate-limited (≥1.05 s gap) with 60 s back-off on errors
 *     and a 7-day negative cache. NewPipe/YouTube search is not ported (no
 *     scraping on device): unresolved tracks hand off as a YTM search URL.
 *  3. Saved manual "wrong song" correction is applied LAST, as an override.
 */
class VideoResolver(
    private val store: EngineStore,
    private val net: NetFetcher?,
    private val allowNetwork: () -> Boolean,
    private val clock: () -> Double = { System.currentTimeMillis() / 1000.0 },
    private val log: (String) -> Unit = {},
) {
    companion object {
        const val MB_MIN_GAP_SEC = 1.05
        const val BACKOFF_SEC = 60.0
        /** Definitive "no video" results are not looked up again for this long. */
        const val NEGATIVE_TTL_SEC = 60L * 24 * 3600
        const val NEG_PREFIX = "neg:resolve:"
        const val USER_AGENT = "MusicFinderAndroid/0.3 (personal on-device recommender)"
        @Volatile private var lastNetAt = 0.0
        @Volatile private var backoffUntil = 0.0
        private val lock = Any()
        private val json = Json { ignoreUnknownKeys = true }
    }

    fun recordCorrection(artist: String, title: String, videoIdOrUrl: String, note: String = "wrong_song"): ResolveResult {
        val vid = YouTubeIds.extract(videoIdOrUrl) ?: videoIdOrUrl.trim()
        if (vid.length != 11) return ResolveResult(false, "", "bad_video_id", "bad_video_id", error = "video_id must be 11-char YouTube id")
        val key = normalizeKey(artist, title)
        val now = clock()
        store.tx {
            store.putCorrection(key, artist, title, vid, note, now)
            store.putResolve(ResolveRow(key, artist, title, vid, 1.0, "user_correction", "ok", "user_correction", null, now))
        }
        return ResolveResult(true, vid, "user_correction", "ok")
    }

    /** True when a network lookup is allowed and worth trying (not negative-cached, not in back-off). */
    fun canLookup(artist: String, title: String): Boolean {
        if (net == null || !allowNetwork()) return false
        val now = clock()
        if (now < backoffUntil && backoffUntil - now <= BACKOFF_SEC) return false
        return !isNegativeCached(normalizeKey(artist.trim(), title.trim()))
    }

    fun isNegativeCached(key: String): Boolean = store.httpCacheGet(NEG_PREFIX + key, clock()) != null

    /** Remember a definitive no-match (from this device or the box pre-resolve) for [ttlSec]. */
    fun putNegative(key: String, status: String, ttlSec: Long = NEGATIVE_TTL_SEC, now: Double = clock()) {
        store.httpCacheSet(NEG_PREFIX + key, buildJsonObject { put("status", status) }.toString(), ttlSec, now)
    }

    fun resolve(artist: String, title: String, candidateVideoId: String = "", allowNetworkLookup: Boolean = true): ResolveResult {
        val a = artist.trim()
        val t = title.trim()
        if (a.isEmpty() || t.isEmpty()) return ResolveResult(false, "", "missing_artist_title", "missing_artist_title")
        val key = normalizeKey(a, t)
        val chain = ArrayList<String>()
        var auto: ResolveResult? = fromLocal(key, a, t, candidateVideoId)?.also { chain += it.source }

        if (auto == null) {
            if (allowNetworkLookup && net != null && allowNetwork() && isNegativeCached(key)) {
                chain += "negative_cache"
            } else if (allowNetworkLookup && net != null && allowNetwork()) {
                chain += "musicbrainz"
                val hit = mbRecordingYoutube(a, t)
                if (hit != null) {
                    chain += hit.source
                    store.putResolve(ResolveRow(key, a, t, hit.videoId, 0.95, t, "ok", hit.source, null, clock()))
                    auto = hit
                }
            } else {
                chain += "offline_or_disabled"
            }
        }
        val corr = store.correction(key)
        if (!corr.isNullOrEmpty()) {
            chain += "user_correction_override"
            return ResolveResult(true, corr, "user_correction", "ok", chain, override = true)
        }
        return (auto ?: ResolveResult(false, "", "no_match", "no_match")).copy(chain = chain)
    }

    private fun fromLocal(key: String, artist: String, title: String, candidateVideoId: String): ResolveResult? {
        val cvid = if (candidateVideoId.isNotEmpty()) YouTubeIds.extract(candidateVideoId) else null
        if (cvid != null && cvid.length == 11) return ResolveResult(true, cvid, "candidate_field", "ok")
        store.resolveCacheOk(key)?.let { return ResolveResult(true, it.videoId, "cache:${it.source.ifEmpty { "prior" }}", "ok") }
        store.seedVideoId(key)?.let { v ->
            val vid = YouTubeIds.extract(v) ?: v
            if (vid.length == 11) return ResolveResult(true, vid, "seeds", "ok")
        }
        store.seedVideoIdByArtistTitle(artist, title)?.let { v ->
            val vid = YouTubeIds.extract(v) ?: v
            if (vid.length == 11) return ResolveResult(true, vid, "seeds_title", "ok")
        }
        return null
    }

    private fun normSimple(s: String): String =
        Py.collapseWs(Py.casefold(s).replace(Regex("[^\\p{L}\\p{N}_\\s]"), " "))

    private fun tokenOverlap(a: String, b: String): Double {
        val at = normSimple(a).split(' ').filter { it.isNotEmpty() }.toSet()
        val bt = normSimple(b).split(' ').filter { it.isNotEmpty() }.toSet()
        if (at.isEmpty() || bt.isEmpty()) return 0.0
        return (at intersect bt).size.toDouble() / maxOf(at.size, bt.size)
    }

    private fun mbGet(url: String): JsonObject {
        val now = clock()
        // Clamp: a clock jump (or a different clock source) must never stall lookups.
        if (now < backoffUntil && backoffUntil - now <= BACKOFF_SEC) throw IllegalStateException("musicbrainz back-off")
        synchronized(lock) {
            val wait = (MB_MIN_GAP_SEC - (clock() - lastNetAt)).coerceIn(0.0, MB_MIN_GAP_SEC)
            if (wait > 0) Thread.sleep((wait * 1000).toLong())
            try {
                val body = net!!.get(url, mapOf("User-Agent" to USER_AGENT, "Accept" to "application/json"))
                return json.parseToJsonElement(body).jsonObject
            } catch (e: Exception) {
                val m = (e.message ?: "").lowercase()
                if ("429" in m || "503" in m || "403" in m || "timeout" in m) backoffUntil = clock() + BACKOFF_SEC
                throw e
            } finally {
                lastNetAt = clock()
            }
        }
    }

    private fun mbRecordingYoutube(artist: String, title: String): ResolveResult? {
        val cacheKey = "mb:ytrels:" + normalizeKey(artist, title)
        store.httpCacheGet(cacheKey, clock())?.let { cached ->
            val vid = runCatching { json.parseToJsonElement(cached).jsonObject["video_id"]?.jsonPrimitive?.contentOrNull }.getOrNull()
            return if (!vid.isNullOrEmpty()) ResolveResult(true, vid, "musicbrainz_url", "ok") else null
        }
        return try {
            val q = YouTubeIds.quotePlus("recording:\"$title\" AND artist:\"$artist\"")
            val data = mbGet("https://musicbrainz.org/ws/2/recording?query=$q&fmt=json&limit=5")
            val recs = (data["recordings"] as? JsonArray) ?: JsonArray(emptyList())
            for (r in recs) {
                val ro = r.jsonObject
                val mbid = ro["id"]?.jsonPrimitive?.contentOrNull ?: continue
                val rt = ro["title"]?.jsonPrimitive?.contentOrNull ?: ""
                if (normSimple(rt) != normSimple(title) && tokenOverlap(rt, title) < 0.7) continue
                val detail = mbGet("https://musicbrainz.org/ws/2/recording/$mbid?fmt=json&inc=url-rels%2Bisrcs")
                for (rel in (detail["relations"] as? JsonArray) ?: JsonArray(emptyList())) {
                    val res = (rel.jsonObject["url"] as? JsonObject)?.get("resource")?.jsonPrimitive?.contentOrNull ?: ""
                    if ("youtu" !in res.lowercase()) continue
                    val vid = YouTubeIds.extract(res) ?: continue
                    store.httpCacheSet(cacheKey, buildJsonObject { put("video_id", vid); put("matched", rt) }.toString(), 30L * 24 * 3600, clock())
                    log("resolve: musicbrainz url-rel $artist – $title → $vid")
                    return ResolveResult(true, vid, "musicbrainz_url", "ok")
                }
            }
            store.httpCacheSet(cacheKey, "{\"video_id\":\"\"}", NEGATIVE_TTL_SEC, clock())
            putNegative(normalizeKey(artist, title), "no_match")
            log("resolve: no video for $artist – $title (cached ${NEGATIVE_TTL_SEC / 86400} d)")
            null
        } catch (e: Exception) {
            log("resolve: musicbrainz failed for $artist – $title: ${e.message}")
            null
        }
    }
}
