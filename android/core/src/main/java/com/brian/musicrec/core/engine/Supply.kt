package com.brian.musicrec.core.engine

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.time.LocalDate

/**
 * One persistent, rate-limited request queue for every optional online call the
 * phone makes (MusicBrainz, Last.fm). Persistent cache (store http cache), a daily
 * request budget and per-host exponential back-off (both in store meta, so they
 * survive restarts), minimum gaps per host, and a MusicBrainz-compliant User-Agent.
 * Never touches any Google account.
 */
class RequestQueue(
    private val store: EngineStore,
    private val net: NetFetcher?,
    private val clock: () -> Double,
    private val log: (String) -> Unit = {},
    var dailyBudget: Int = 500,
    private val sleeper: (Long) -> Unit = { Thread.sleep(it) },
) {
    companion object {
        const val USER_AGENT = "MusicFinderAndroid/0.5.0 (personal non-commercial music recommender; https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)"
        val HOST_GAP_SEC = mapOf("musicbrainz.org" to 1.1, "ws.audioscrobbler.com" to 0.25)
        const val BACKOFF_BASE_SEC = 60.0
        const val BACKOFF_MAX_SEC = 6 * 3600.0
    }

    private val lock = Any()
    private val lastAt = HashMap<String, Double>()
    var lastSkipReason: String = ""
        private set

    private fun host(url: String) = url.substringAfter("://").substringBefore('/')
    private fun dayKey() = "rq_budget:${(clock() / 86400).toLong()}"

    fun usedToday(): Int = store.meta(dayKey())?.toIntOrNull() ?: 0
    fun budgetLeft(): Int = maxOf(0, dailyBudget - usedToday())

    fun backoffUntil(host: String): Double = store.meta("rq_backoff:$host")?.substringBefore('|')?.toDoubleOrNull() ?: 0.0
    private fun backoffLevel(host: String): Int = store.meta("rq_backoff:$host")?.substringAfter('|', "0")?.toIntOrNull() ?: 0

    /**
     * GET [url]; cached under [cacheKey] for [ttlSec] (0 = no cache). Returns null when
     * skipped (no network, budget spent, host in back-off) or on error — see [lastSkipReason].
     */
    fun get(url: String, cacheKey: String? = null, ttlSec: Long = 0, headers: Map<String, String> = emptyMap()): String? =
        fetch(url, cacheKey, ttlSec, headers).first

    /** Like [get] but throws with the skip/error reason (thread-safe reason, for the video resolver). */
    fun getOrThrow(url: String, headers: Map<String, String> = emptyMap()): String {
        val (body, reason) = fetch(url, null, 0, headers)
        return body ?: throw IllegalStateException("request queue: $reason")
    }

    fun status(): Map<String, Any> = mapOf(
        "used_today" to usedToday(), "daily_budget" to dailyBudget,
        "backoff" to HOST_GAP_SEC.keys.associateWith { h -> maxOf(0.0, backoffUntil(h) - clock()).toLong() },
        "user_agent" to USER_AGENT,
    )

    private fun fetch(url: String, cacheKey: String?, ttlSec: Long, headers: Map<String, String>): Pair<String?, String> {
        cacheKey?.let { k -> store.httpCacheGet("rq:$k", clock())?.let { return it to "" } }
        val n = net ?: run { lastSkipReason = "offline"; return null to "offline" }
        val h = host(url)
        synchronized(lock) {
            val now = clock()
            val until = backoffUntil(h)
            if (now < until && until - now <= BACKOFF_MAX_SEC) { lastSkipReason = "backoff:$h"; return null to "backoff:$h" }
            if (budgetLeft() <= 0) { lastSkipReason = "daily_budget"; return null to "daily_budget" }
            val gap = HOST_GAP_SEC[h] ?: 0.5
            val wait = (gap - (now - (lastAt[h] ?: 0.0))).coerceIn(0.0, gap)
            if (wait > 0) sleeper((wait * 1000).toLong())
            store.setMeta(dayKey(), (usedToday() + 1).toString())
            try {
                val body = n.get(url, mapOf("User-Agent" to USER_AGENT, "Accept" to "application/json") + headers)
                store.setMeta("rq_backoff:$h", "0|0")
                cacheKey?.let { store.httpCacheSet("rq:$it", body, ttlSec, clock()) }
                lastSkipReason = ""
                return body to ""
            } catch (e: Exception) {
                val m = (e.message ?: "").lowercase()
                if (listOf("429", "503", "502", "504", "500", "timeout", "timed out").any { it in m }) {
                    val lvl = backoffLevel(h) + 1
                    val sec = minOf(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (1 shl minOf(lvl - 1, 10)))
                    store.setMeta("rq_backoff:$h", "${clock() + sec}|$lvl")
                    log("queue: $h back-off ${sec.toLong()} s after: ${e.message}")
                }
                lastSkipReason = "error:${e.message}"
                return null to "error:${e.message}"
            } finally {
                lastAt[h] = clock()
            }
        }
    }
}

/**
 * On-device candidate refill (no AI/LLM, no Google account):
 *  - Last.fm track.getSimilar for recent Loved/Liked tracks and artist.getTopTracks
 *    for liked artists → proven pool (skipped gracefully without an API key);
 *  - MusicBrainz recordings by liked artists with `firstreleasedate` in the last 90
 *    days → fresh pool (first-release-date is the recording's, so reissues stay out);
 *  - Last.fm listeners (track + artist) for Loved/Liked tracks → Friends broad appeal.
 * Every request goes through [RequestQueue]. Returns an auditable funnel.
 */
class Refill(
    private val store: EngineStore,
    private val queue: RequestQueue,
    private val clock: () -> Double,
    private val today: () -> LocalDate,
    private val log: (String) -> Unit = {},
) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    data class Report(val funnel: Map<String, Map<String, Int>>, val notes: List<String>) {
        fun added(): Int = funnel.values.sumOf { it["added"] ?: 0 }
    }

    private fun q(s: String) = YouTubeIds.quotePlus(s)
    private fun parse(body: String?): JsonObject? = body?.let { runCatching { json.parseToJsonElement(it).jsonObject }.getOrNull() }
    private fun JsonElement?.str(k: String) = ((this as? JsonObject)?.get(k) as? JsonPrimitive)?.contentOrNull ?: ""
    private fun arrOf(e: JsonElement?): List<JsonElement> = when (e) {
        is JsonArray -> e.toList()
        is JsonObject -> listOf(e)
        else -> emptyList()
    }

    fun run(lastfmKey: String, likedTracks: List<Pair<String, String>>, likedArtists: List<String>, maxNew: Int = 80): Report {
        val notes = ArrayList<String>()
        val funnel = LinkedHashMap<String, LinkedHashMap<String, Int>>()
        fun bump(src: String, k: String, n: Int = 1) { funnel.getOrPut(src) { linkedMapOf() }.merge(k, n, Int::plus) }
        val blocked = store.blockedKeys()
        val blockedVids = store.blockedVideoIds()
        val skip = SeedImporter.skipList(store)
        val inPool = HashSet<String>().apply { for (l in listOf("proven", "fresh")) store.candidates(l).forEach { add(it.key()) } }
        var added = 0

        fun offer(src: String, lane: String, c: Candidate) {
            bump(src, "fetched")
            val k = c.key()
            when {
                c.artist.isBlank() || c.title.isBlank() -> bump(src, "rejected_empty")
                k in inPool -> bump(src, "rejected_duplicate")
                !SeedImporter.cleanCandidate(c, skip, blocked, blockedVids) -> bump(src, "rejected_blocked_or_junk")
                added >= maxNew -> bump(src, "rejected_cap")
                else -> {
                    c.cluster = Clusters.assign(c.tags, c.artist, c.title)
                    store.putCandidate(lane, c); inPool += k; added++; bump(src, "added")
                }
            }
        }

        // ---- Last.fm (optional) ----
        if (lastfmKey.isBlank()) {
            notes += "Last.fm key not set — Last.fm sources skipped"
        } else {
            val base = "https://ws.audioscrobbler.com/2.0/?format=json&api_key=${q(lastfmKey)}"
            for ((a, t) in likedTracks.take(5)) {
                val body = queue.get("$base&method=track.getsimilar&artist=${q(a)}&track=${q(t)}&limit=15",
                    "lfm:similar:${normalizeKey(a, t)}", 7L * 86400)
                if (body == null) { bump("lastfm_similar", "request_skipped"); continue }
                for (e in arrOf(parse(body)?.get("similartracks")?.let { (it as? JsonObject)?.get("track") })) {
                    val name = e.str("name"); val artist = (e as JsonObject)["artist"].str("name")
                    val match = (e["match"] as? JsonPrimitive)?.contentOrNull?.toDoubleOrNull() ?: 0.5
                    offer("lastfm_similar", "proven", Candidate(artist, name,
                        sourceSignals = linkedMapOf("lastfm" to match.coerceIn(0.3, 1.0), "artist_overlap" to 0.3),
                        relatedToArtists = listOf(a)))
                }
            }
            for (a in likedArtists.take(8)) {
                val body = queue.get("$base&method=artist.gettoptracks&artist=${q(a)}&limit=6",
                    "lfm:top:${Affinity.artistKey(a)}", 7L * 86400)
                if (body == null) { bump("lastfm_top", "request_skipped"); continue }
                for (e in arrOf(parse(body)?.get("toptracks")?.let { (it as? JsonObject)?.get("track") })) {
                    val name = e.str("name")
                    val listeners = e.str("listeners").toDoubleOrNull()
                    offer("lastfm_top", "proven", Candidate(a, name,
                        sourceSignals = linkedMapOf("lastfm" to 0.8, "artist_overlap" to 0.9),
                        relatedToArtists = listOf(a), popularity = listeners))
                    if (listeners != null) store.setMeta("lfm_listeners:track:${normalizeKey(a, name)}", listeners.toString())
                }
            }
            // listeners for broad appeal (Friends mode)
            for ((a, t) in likedTracks.take(30)) {
                val tk = normalizeKey(a, t)
                if (store.meta("lfm_listeners:track:$tk") == null) {
                    val o = parse(queue.get("$base&method=track.getinfo&artist=${q(a)}&track=${q(t)}", "lfm:tinfo:$tk", 30L * 86400))
                    o?.get("track").str("listeners").toDoubleOrNull()?.let { store.setMeta("lfm_listeners:track:$tk", it.toString()) }
                }
                val ak = Affinity.artistKey(a)
                if (store.meta("lfm_listeners:artist:$ak") == null) {
                    val o = parse(queue.get("$base&method=artist.getinfo&artist=${q(a)}", "lfm:ainfo:$ak", 30L * 86400))
                    ((o?.get("artist") as? JsonObject)?.get("stats")).str("listeners").toDoubleOrNull()
                        ?.let { store.setMeta("lfm_listeners:artist:$ak", it.toString()) }
                }
            }
        }

        // ---- MusicBrainz new recordings by liked artists (fresh) ----
        val cutoff = today().minusDays(90).toString()
        for (a in likedArtists.take(8)) {
            val ak = Affinity.artistKey(a)
            val art = parse(queue.get("https://musicbrainz.org/ws/2/artist?query=${q("artist:\"$a\"")}&fmt=json&limit=1",
                "mb:artist:$ak", 30L * 86400))
            val first = (art?.get("artists") as? JsonArray)?.firstOrNull() as? JsonObject
            val mbid = first.str("id")
            if (mbid.isEmpty() || Affinity.artistKey(first.str("name")) != ak) { bump("musicbrainz_fresh", "artist_not_found"); continue }
            val body = queue.get("https://musicbrainz.org/ws/2/recording?query=${q("arid:$mbid AND firstreleasedate:[$cutoff TO *]")}&fmt=json&limit=25",
                "mb:recent:$mbid:$cutoff", 86400)
            if (body == null) { bump("musicbrainz_fresh", "request_skipped"); continue }
            for (r in arrOf(parse(body)?.get("recordings"))) {
                val o = r as? JsonObject ?: continue
                val frd = o.str("first-release-date")
                if (frd.length < 10 || frd < cutoff) { bump("musicbrainz_fresh", "fetched"); bump("musicbrainz_fresh", "rejected_old_or_undated"); continue }
                val c = Candidate(a, o.str("title"), releaseDate = frd,
                    sourceSignals = linkedMapOf("musicbrainz_fresh" to 1.0, "freshness" to 1.0, "artist_overlap" to 0.9),
                    relatedToArtists = listOf(a))
                c.externalIds["original_date"] = frd
                c.externalIds["mb_recording"] = o.str("id")
                offer("musicbrainz_fresh", "fresh", c)
            }
        }
        if (queue.lastSkipReason.isNotEmpty()) notes += "last queue state: ${queue.lastSkipReason}"
        notes += "budget left today: ${queue.budgetLeft()}"
        val report = Report(funnel, notes)
        store.setMeta("refill_last", buildJsonObject {
            put("at", clock()); put("added", report.added()); put("funnel", funnel.toString()); put("notes", notes.joinToString("; "))
        }.toString())
        log("refill: added ${report.added()} $funnel ${notes.joinToString("; ")}")
        return report
    }
}

/** Friends mode broad appeal B (parity with `engine/friends.py`). Missing component = 0.5. */
object BroadAppeal {
    const val MIN_B = 0.70
    val TAG_MAINSTREAM = mapOf(
        "pop" to 0.9, "dance" to 0.8, "hip hop" to 0.85, "rap" to 0.8, "rock" to 0.8, "r&b" to 0.8, "rnb" to 0.8,
        "country" to 0.75, "latin" to 0.75, "electronic" to 0.6, "edm" to 0.75, "house" to 0.65, "vocal" to 0.6,
        "soundtrack" to 0.55, "indie" to 0.5, "piano" to 0.5, "game" to 0.45, "trance" to 0.45, "jazz" to 0.4,
        "classical" to 0.4, "folk" to 0.4, "instrumental" to 0.4, "metal" to 0.35, "ambient" to 0.3, "ragtime" to 0.25,
    )
    val REACTION_UTILITY = mapOf("liked" to 1.0, "didnt_work" to -1.0, "mixed" to 0.25)

    /** Fraction of the reference values ≤ x; null when unknown. */
    fun ecdf(sortedRef: List<Double>, x: Double?): Double? {
        if (x == null || sortedRef.isEmpty()) return null
        var lo = 0; var hi = sortedRef.size
        while (lo < hi) { val mid = (lo + hi) / 2; if (sortedRef[mid] <= x) lo = mid + 1 else hi = mid }
        return lo.toDouble() / sortedRef.size
    }

    fun tagMainstream(tags: List<String>): Double? {
        val vals = tags.mapNotNull { TAG_MAINSTREAM[Py.casefold(it).trim()] }
        return if (vals.isEmpty()) null else vals.sum() / vals.size
    }

    fun appeal(trackListeners: Double?, artistListeners: Double?, tags: List<String>, refTrack: List<Double>, refArtist: List<Double>): Double {
        val t = ecdf(refTrack, trackListeners) ?: 0.5
        val a = ecdf(refArtist, artistListeners) ?: 0.5
        val m = tagMainstream(tags) ?: 0.5
        return 0.5 * t + 0.3 * a + 0.2 * m
    }
}
