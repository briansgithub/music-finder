package com.brian.musicrec.core.engine

data class WeightRow(val alpha: Double, val beta: Double, val weight: Double)

data class TagRow(
    val tag: String,
    val kind: String,
    val count: Int,
    val asDial: Boolean,
    val firstSeen: Double? = null,
    val lastSeen: Double? = null,
)

data class RatingRow(
    val artist: String,
    val title: String,
    val key: String,
    val rating: String,
    val lane: String = "",
    val cluster: String = "",
    val predictedConfidence: Double? = null,
    val createdAt: Double,
)

data class LogRow(
    val artist: String,
    val title: String,
    val key: String,
    val lane: String,
    val confidence: Double?,
    val why: String,
    val whyJson: String,
    val metricsSnapshot: String,
    val createdAt: Double,
)

data class ResolveRow(
    val key: String,
    val artist: String,
    val title: String,
    val videoId: String,
    val score: Double?,
    val matched: String?,
    val status: String,
    val source: String,
    val error: String? = null,
    val updatedAt: Double,
)

data class FeedbackRow(
    val id: Long,
    val key: String,
    val event: String,
    val artist: String,
    val title: String,
    val detail: String,
    val lane: String,
    val createdAt: Double,
)

data class SeedRow(
    val artist: String,
    val title: String,
    val key: String,
    val source: String,
    val videoId: String,
    val holdout: Boolean = false,
)

/**
 * Storage the on-device engine needs. Mirrors the SQL the Python engine runs
 * against `recommender.db`. Android uses [com.brian.musicrec.core.android.SqliteEngineStore];
 * JVM tests use [InMemoryEngineStore].
 */
interface EngineStore {
    fun <T> tx(block: () -> T): T

    // seeds / blocklist
    /** `db.seed_artists`: strong, non-holdout seed artists ordered by seed count desc. */
    fun seedArtists(): List<String>
    /** Non-holdout seed count per artist (seed-similarity profile). */
    fun seedArtistCounts(): Map<String, Int>
    /** All meta rows whose key starts with [prefix] (e.g. Last.fm listener counts). */
    fun metaWithPrefix(prefix: String): Map<String, String>
    /** Seeds from YouTube likes / liked lists (Friends-mode "Liked" playback candidates). */
    fun likedSeeds(limit: Int = 2000): List<SeedRow>
    /** `originality.build_blocklist` keys: every seed + rated + already-recommended key. */
    fun blockedKeys(): Set<String>
    fun blockedVideoIds(): Set<String>
    fun seedVideoId(key: String): String?
    fun seedVideoIdByArtistTitle(artist: String, title: String): String?
    fun putSeed(row: SeedRow)
    fun putBlockedKey(key: String)
    fun putBlockedVideoId(videoId: String)

    // candidate pool
    fun candidates(pool: String): List<Candidate>
    fun putCandidate(pool: String, c: Candidate)
    fun clearCandidates()
    fun candidateCounts(): Map<String, Int>
    fun metricsFor(key: String): Map<String, MetricValue>?
    fun putMetrics(key: String, metrics: Map<String, MetricValue>)

    // learned weights
    fun getWeight(scope: String, feature: String): WeightRow?
    fun putWeight(scope: String, feature: String, row: WeightRow, updatedAt: Double)
    fun insertWeightIfAbsent(scope: String, feature: String, row: WeightRow, updatedAt: Double)
    fun weightCount(): Int
    fun deleteWeight(scope: String, feature: String)
    /** All rows of one scope (feature → row), e.g. scope "artist" for Dislike down-weights. */
    fun weightsInScope(scope: String): Map<String, WeightRow>

    // calibration
    fun latestCalibration(): Calibration.Calib?
    fun saveCalibration(c: Calibration.Calib, updatedAt: Double)

    // ratings / feedback / recommendation log
    /** Returns the new row id. */
    fun addRating(row: RatingRow): Long
    fun deleteRating(id: Long)
    fun ratings(limit: Int = 500): List<RatingRow>
    fun ratingExists(key: String, createdAt: Double): Boolean
    fun addFeedback(key: String, event: String, artist: String, title: String, detail: String, lane: String, createdAt: Double): Long
    fun deleteFeedback(id: Long)
    /** Oldest → newest feedback rows of one event type. */
    fun feedbackRows(event: String): List<FeedbackRow>
    fun logRecommendation(row: LogRow): Long
    fun deleteLog(id: Long)
    fun lastLog(key: String): LogRow?
    fun logCount(): Int

    // tag vocabulary
    fun getTag(tag: String, kind: String): TagRow?
    fun putTag(row: TagRow)
    fun deleteTag(tag: String, kind: String)
    /** ORDER BY count DESC, tag ASC (binary) LIMIT n */
    fun listTags(kind: String? = null, dialsOnly: Boolean = false, limit: Int = 200): List<TagRow>

    // video-ID resolve
    /** Successful automatic resolve (excludes user_correction rows). */
    fun resolveCacheOk(key: String): ResolveRow?
    fun putResolve(row: ResolveRow)
    fun correction(key: String): String?
    fun putCorrection(key: String, artist: String, title: String, videoId: String, note: String, updatedAt: Double)
    fun httpCacheGet(key: String, now: Double): String?
    fun httpCacheSet(key: String, payload: String, ttlSeconds: Long, now: Double)

    // misc
    fun meta(key: String): String?
    fun setMeta(key: String, value: String)
}

/** Seed sources that are the user's own likes (YouTube likes / LL liked list). */
val LIKED_SEED_SOURCES = setOf("yt_liked", "ll_liked", "liked_ll", "yt_ll")

/** Pure-JVM store for tests (and a fallback if SQLite cannot open). */
class InMemoryEngineStore : EngineStore {
    private val seeds = ArrayList<SeedRow>()
    private val blocked = HashSet<String>()
    private val blockedVids = HashSet<String>()
    private val pool = LinkedHashMap<String, LinkedHashMap<String, Candidate>>()
    private val metrics = HashMap<String, Map<String, MetricValue>>()
    private val weights = LinkedHashMap<Pair<String, String>, WeightRow>()
    private var calib: Calibration.Calib? = null
    private val ratingRows = ArrayList<RatingRow>()
    private val ratingIds = ArrayList<Long>()
    val feedback = ArrayList<Map<String, Any?>>()
    private val log = ArrayList<LogRow>()
    private val logIds = ArrayList<Long>()
    private var nextId = 1L
    private val tags = LinkedHashMap<Pair<String, String>, TagRow>()
    private val resolve = java.util.concurrent.ConcurrentHashMap<String, ResolveRow>()
    private val corrections = java.util.concurrent.ConcurrentHashMap<String, String>()
    private val httpCache = java.util.concurrent.ConcurrentHashMap<String, Triple<String, Double, Long>>()
    private val metaMap = HashMap<String, String>()

    override fun <T> tx(block: () -> T): T = synchronized(this) { block() }

    override fun seedArtists(): List<String> {
        val counts = LinkedHashMap<String, Int>()
        for (s in seeds) {
            if (s.holdout || s.artist.isEmpty() || s.artist == "Unknown") continue
            counts[s.artist] = (counts[s.artist] ?: 0) + 1
        }
        return counts.entries.sortedByDescending { it.value }.map { it.key }
    }

    override fun metaWithPrefix(prefix: String): Map<String, String> = metaMap.filterKeys { it.startsWith(prefix) }

    override fun seedArtistCounts(): Map<String, Int> {
        val counts = LinkedHashMap<String, Int>()
        for (s in seeds) {
            if (s.holdout || s.artist.isEmpty() || s.artist == "Unknown") continue
            counts[s.artist] = (counts[s.artist] ?: 0) + 1
        }
        return counts
    }

    override fun likedSeeds(limit: Int): List<SeedRow> =
        seeds.filter { it.source in LIKED_SEED_SOURCES && it.artist.isNotEmpty() && it.artist != "Unknown" }.take(limit)

    override fun feedbackRows(event: String): List<FeedbackRow> = feedback.filter { it["event"] == event }.map {
        FeedbackRow(it["id"] as Long, it["key"] as String, event, it["artist"] as String, it["title"] as String,
            it["detail"] as String, it["lane"] as String, it["created_at"] as Double)
    }

    override fun blockedKeys(): Set<String> =
        blocked + seeds.map { it.key } + ratingRows.map { it.key } + log.map { it.key }

    override fun blockedVideoIds(): Set<String> = blockedVids + seeds.mapNotNull { it.videoId.ifEmpty { null } }

    private val sourceOrder = mapOf("yt_liked" to 0, "yt_library" to 1)
    override fun seedVideoId(key: String): String? = seeds.withIndex()
        .filter { it.value.key == key && it.value.videoId.isNotEmpty() }
        .sortedWith(compareBy({ sourceOrder[it.value.source] ?: 2 }, { it.index }))
        .firstOrNull()?.value?.videoId

    override fun seedVideoIdByArtistTitle(artist: String, title: String): String? =
        seeds.firstOrNull { it.artist == artist && it.title == title && it.videoId.isNotEmpty() }?.videoId

    override fun putSeed(row: SeedRow) { seeds += row }
    override fun putBlockedKey(key: String) { blocked += key }
    override fun putBlockedVideoId(videoId: String) { blockedVids += videoId }

    override fun candidates(pool: String): List<Candidate> = this.pool[pool]?.values?.map { it.deepCopy() } ?: emptyList()
    override fun putCandidate(pool: String, c: Candidate) {
        this.pool.getOrPut(pool) { LinkedHashMap() }[c.key()] = c.deepCopy()
    }
    override fun clearCandidates() = pool.clear()
    override fun candidateCounts(): Map<String, Int> = pool.mapValues { it.value.size }
    override fun metricsFor(key: String) = metrics[key]
    override fun putMetrics(key: String, metrics: Map<String, MetricValue>) { this.metrics[key] = metrics }

    override fun getWeight(scope: String, feature: String) = weights[scope to feature]
    override fun putWeight(scope: String, feature: String, row: WeightRow, updatedAt: Double) { weights[scope to feature] = row }
    override fun insertWeightIfAbsent(scope: String, feature: String, row: WeightRow, updatedAt: Double) {
        weights.putIfAbsent(scope to feature, row)
    }
    override fun weightCount() = weights.size
    /** Test/debug helper: full copy of model_weights. */
    fun allWeights(): Map<Pair<String, String>, WeightRow> = LinkedHashMap(weights)
    fun allTags(): Map<Pair<String, String>, TagRow> = LinkedHashMap(tags)
    override fun deleteWeight(scope: String, feature: String) { weights.remove(scope to feature) }
    override fun weightsInScope(scope: String): Map<String, WeightRow> =
        weights.filterKeys { it.first == scope }.mapKeys { it.key.second }

    override fun latestCalibration() = calib
    override fun saveCalibration(c: Calibration.Calib, updatedAt: Double) { calib = c }

    override fun addRating(row: RatingRow): Long { val id = nextId++; ratingRows += row; ratingIds += id; return id }
    override fun deleteRating(id: Long) {
        val i = ratingIds.indexOf(id)
        if (i >= 0) { ratingIds.removeAt(i); ratingRows.removeAt(i) }
    }
    override fun ratings(limit: Int) = ratingRows.asReversed().take(limit)
    override fun ratingExists(key: String, createdAt: Double) = ratingRows.any { it.key == key && it.createdAt == createdAt }
    override fun addFeedback(key: String, event: String, artist: String, title: String, detail: String, lane: String, createdAt: Double): Long {
        val id = nextId++
        feedback += mapOf("id" to id, "key" to key, "event" to event, "artist" to artist, "title" to title, "detail" to detail, "lane" to lane, "created_at" to createdAt)
        return id
    }
    override fun deleteFeedback(id: Long) { feedback.removeAll { it["id"] == id } }
    override fun logRecommendation(row: LogRow): Long { val id = nextId++; log += row; logIds += id; return id }
    override fun deleteLog(id: Long) {
        val i = logIds.indexOf(id)
        if (i >= 0) { logIds.removeAt(i); log.removeAt(i) }
    }
    override fun lastLog(key: String) = log.lastOrNull { it.key == key }
    override fun logCount() = log.size

    override fun getTag(tag: String, kind: String) = tags[tag to kind]
    override fun putTag(row: TagRow) { tags[row.tag to row.kind] = row }
    override fun deleteTag(tag: String, kind: String) { tags.remove(tag to kind) }
    override fun listTags(kind: String?, dialsOnly: Boolean, limit: Int): List<TagRow> = tags.values
        .filter { (kind == null || it.kind == kind) && (!dialsOnly || it.asDial) }
        .sortedWith(compareByDescending<TagRow> { it.count }.thenComparator { a, b -> utf8Compare(a.tag, b.tag) })
        .take(limit)

    override fun resolveCacheOk(key: String): ResolveRow? = resolve[key]?.takeIf {
        it.status == "ok" && it.videoId.isNotEmpty() && it.source !in setOf("user_correction", "user_correction_override")
    }
    override fun putResolve(row: ResolveRow) { resolve[row.key] = row }
    override fun correction(key: String) = corrections[key]
    override fun putCorrection(key: String, artist: String, title: String, videoId: String, note: String, updatedAt: Double) {
        corrections[key] = videoId
    }
    override fun httpCacheGet(key: String, now: Double): String? {
        val (p, created, ttl) = httpCache[key] ?: return null
        return if (now - created > ttl) null else p
    }
    override fun httpCacheSet(key: String, payload: String, ttlSeconds: Long, now: Double) {
        httpCache[key] = Triple(payload, now, ttlSeconds)
    }
    override fun meta(key: String) = metaMap[key]
    override fun setMeta(key: String, value: String) { metaMap[key] = value }
}

/** SQLite's default BINARY collation compares UTF-8 bytes. */
fun utf8Compare(a: String, b: String): Int {
    val x = a.toByteArray(Charsets.UTF_8)
    val y = b.toByteArray(Charsets.UTF_8)
    for (i in 0 until minOf(x.size, y.size)) {
        val d = (x[i].toInt() and 0xff) - (y[i].toInt() and 0xff)
        if (d != 0) return d
    }
    return x.size - y.size
}
