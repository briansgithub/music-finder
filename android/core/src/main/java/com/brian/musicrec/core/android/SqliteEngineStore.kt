package com.brian.musicrec.core.android

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import com.brian.musicrec.core.engine.Calibration
import com.brian.musicrec.core.engine.Candidate
import com.brian.musicrec.core.engine.EngineStore
import com.brian.musicrec.core.engine.JsonCodec
import com.brian.musicrec.core.engine.LogRow
import com.brian.musicrec.core.engine.MetricValue
import com.brian.musicrec.core.engine.RatingRow
import com.brian.musicrec.core.engine.ResolveRow
import com.brian.musicrec.core.engine.SeedRow
import com.brian.musicrec.core.engine.TagRow
import com.brian.musicrec.core.engine.WeightRow
import com.brian.musicrec.core.engine.FeedbackRow
import com.brian.musicrec.core.engine.LIKED_SEED_SOURCES
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/**
 * Android SQLite store with the same tables as the PC engine's recommender.db
 * (ratings incl. predicted_confidence, recommendations_log, model_weights, …)
 * plus `candidates` (the shipped/imported pool) and `meta`.
 */
class SqliteEngineStore(context: Context, name: String = "musicfinder_engine.db") :
    SQLiteOpenHelper(context, name, null, 1), EngineStore {

    override fun onCreate(db: SQLiteDatabase) {
        listOf(
            """CREATE TABLE seeds (id INTEGER PRIMARY KEY, artist TEXT NOT NULL, title TEXT NOT NULL,
               key TEXT NOT NULL, source TEXT, video_id TEXT, holdout INTEGER NOT NULL DEFAULT 0)""",
            "CREATE INDEX idx_seeds_key ON seeds(key)",
            "CREATE TABLE blocked_keys (key TEXT PRIMARY KEY)",
            "CREATE TABLE blocked_video_ids (video_id TEXT PRIMARY KEY)",
            """CREATE TABLE candidates (pool TEXT NOT NULL, key TEXT NOT NULL, position INTEGER NOT NULL,
               payload TEXT NOT NULL, PRIMARY KEY(pool, key))""",
            "CREATE TABLE track_metrics (key TEXT PRIMARY KEY, metrics_json TEXT NOT NULL)",
            """CREATE TABLE model_weights (scope TEXT NOT NULL, feature TEXT NOT NULL, alpha REAL NOT NULL DEFAULT 1.0,
               beta REAL NOT NULL DEFAULT 1.0, weight REAL NOT NULL DEFAULT 0.0, updated_at REAL NOT NULL,
               PRIMARY KEY(scope, feature))""",
            "CREATE TABLE calibration (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL)",
            """CREATE TABLE ratings (id INTEGER PRIMARY KEY, artist TEXT NOT NULL, title TEXT NOT NULL, key TEXT NOT NULL,
               rating TEXT NOT NULL, lane TEXT, cluster TEXT, predicted_confidence REAL, created_at REAL NOT NULL)""",
            "CREATE INDEX idx_ratings_key ON ratings(key)",
            """CREATE TABLE feedback_events (id INTEGER PRIMARY KEY, key TEXT NOT NULL, artist TEXT, title TEXT,
               event TEXT NOT NULL, detail TEXT, lane TEXT, created_at REAL NOT NULL)""",
            """CREATE TABLE recommendations_log (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, key TEXT, lane TEXT,
               confidence REAL, why TEXT, why_json TEXT, metrics_snapshot TEXT, created_at REAL)""",
            "CREATE INDEX idx_log_key ON recommendations_log(key)",
            """CREATE TABLE tag_vocabulary (tag TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'tag', count INTEGER NOT NULL DEFAULT 0,
               first_seen REAL, last_seen REAL, as_dial INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tag, kind))""",
            """CREATE TABLE yt_play_resolve (key TEXT PRIMARY KEY, artist TEXT, title TEXT, video_id TEXT, score REAL,
               matched_name TEXT, status TEXT, source TEXT, raw_error TEXT, updated_at REAL)""",
            """CREATE TABLE yt_play_corrections (key TEXT PRIMARY KEY, artist TEXT, title TEXT, video_id TEXT NOT NULL,
               note TEXT, updated_at REAL)""",
            "CREATE TABLE cache (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL, ttl_seconds INTEGER NOT NULL)",
            "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)",
        ).forEach { db.execSQL(it) }
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit

    private val db: SQLiteDatabase get() = writableDatabase
    private val json = Json { ignoreUnknownKeys = true }

    override fun <T> tx(block: () -> T): T {
        val d = db
        d.beginTransaction()
        try {
            val r = block()
            d.setTransactionSuccessful()
            return r
        } finally {
            d.endTransaction()
        }
    }

    private inline fun <T> query(sql: String, args: Array<String> = emptyArray(), map: (Cursor) -> T): List<T> {
        val out = ArrayList<T>()
        db.rawQuery(sql, args).use { c -> while (c.moveToNext()) out += map(c) }
        return out
    }

    private fun Cursor.str(i: Int): String = if (isNull(i)) "" else getString(i)
    private fun Cursor.dblOrNull(i: Int): Double? = if (isNull(i)) null else getDouble(i)

    override fun seedArtists(): List<String> {
        meta("seed_artists")?.let { s ->
            val list = runCatching { JsonCodec.strList(json.parseToJsonElement(s)) }.getOrDefault(emptyList())
            if (list.isNotEmpty()) return list
        }
        return query(
            "SELECT artist, COUNT(*) c FROM seeds WHERE artist!='' AND artist!='Unknown' AND holdout=0 GROUP BY artist ORDER BY c DESC",
        ) { it.str(0) }
    }

    override fun blockedKeys(): Set<String> = query(
        "SELECT key FROM blocked_keys UNION SELECT key FROM seeds UNION SELECT key FROM ratings " +
            "UNION SELECT key FROM recommendations_log WHERE key IS NOT NULL",
    ) { it.str(0) }.toHashSet()

    override fun blockedVideoIds(): Set<String> = query(
        "SELECT video_id FROM blocked_video_ids UNION SELECT video_id FROM seeds WHERE IFNULL(video_id,'')!=''",
    ) { it.str(0) }.toHashSet()

    override fun seedVideoId(key: String): String? = query(
        "SELECT video_id FROM seeds WHERE key=? AND IFNULL(video_id,'')!='' " +
            "ORDER BY CASE source WHEN 'yt_liked' THEN 0 WHEN 'yt_library' THEN 1 ELSE 2 END, id LIMIT 1",
        arrayOf(key),
    ) { it.str(0) }.firstOrNull()

    override fun seedVideoIdByArtistTitle(artist: String, title: String): String? = query(
        "SELECT video_id FROM seeds WHERE artist=? AND title=? AND IFNULL(video_id,'')!='' LIMIT 1",
        arrayOf(artist, title),
    ) { it.str(0) }.firstOrNull()

    override fun putSeed(row: SeedRow) {
        db.insert("seeds", null, ContentValues().apply {
            put("artist", row.artist); put("title", row.title); put("key", row.key)
            put("source", row.source); put("video_id", row.videoId); put("holdout", if (row.holdout) 1 else 0)
        })
    }

    override fun putBlockedKey(key: String) {
        db.insertWithOnConflict("blocked_keys", null, ContentValues().apply { put("key", key) }, SQLiteDatabase.CONFLICT_IGNORE)
    }

    override fun putBlockedVideoId(videoId: String) {
        db.insertWithOnConflict("blocked_video_ids", null, ContentValues().apply { put("video_id", videoId) }, SQLiteDatabase.CONFLICT_IGNORE)
    }

    override fun candidates(pool: String): List<Candidate> =
        query("SELECT payload FROM candidates WHERE pool=? ORDER BY position", arrayOf(pool)) {
            JsonCodec.candidate(json.parseToJsonElement(it.str(0)) as JsonObject)
        }

    override fun putCandidate(pool: String, c: Candidate) {
        val pos = query("SELECT COUNT(*) FROM candidates WHERE pool=?", arrayOf(pool)) { it.getInt(0) }.first()
        db.insertWithOnConflict("candidates", null, ContentValues().apply {
            put("pool", pool); put("key", c.key()); put("position", pos)
            put("payload", JsonCodec.candidateJson(c).toString())
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun clearCandidates() { db.delete("candidates", null, null) }

    override fun candidateCounts(): Map<String, Int> =
        query("SELECT pool, COUNT(*) FROM candidates GROUP BY pool") { it.str(0) to it.getInt(1) }.toMap()

    override fun metricsFor(key: String): Map<String, MetricValue>? =
        query("SELECT metrics_json FROM track_metrics WHERE key=?", arrayOf(key)) {
            JsonCodec.metrics(json.parseToJsonElement(it.str(0)))
        }.firstOrNull()

    override fun putMetrics(key: String, metrics: Map<String, MetricValue>) {
        db.insertWithOnConflict("track_metrics", null, ContentValues().apply {
            put("key", key); put("metrics_json", JsonCodec.metricsJson(metrics).toString())
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun getWeight(scope: String, feature: String): WeightRow? =
        query("SELECT alpha, beta, weight FROM model_weights WHERE scope=? AND feature=?", arrayOf(scope, feature)) {
            WeightRow(it.getDouble(0), it.getDouble(1), it.getDouble(2))
        }.firstOrNull()

    private fun weightValues(scope: String, feature: String, row: WeightRow, ts: Double) = ContentValues().apply {
        put("scope", scope); put("feature", feature); put("alpha", row.alpha); put("beta", row.beta)
        put("weight", row.weight); put("updated_at", ts)
    }

    override fun putWeight(scope: String, feature: String, row: WeightRow, updatedAt: Double) {
        db.insertWithOnConflict("model_weights", null, weightValues(scope, feature, row, updatedAt), SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun insertWeightIfAbsent(scope: String, feature: String, row: WeightRow, updatedAt: Double) {
        db.insertWithOnConflict("model_weights", null, weightValues(scope, feature, row, updatedAt), SQLiteDatabase.CONFLICT_IGNORE)
    }

    override fun deleteWeight(scope: String, feature: String) {
        db.delete("model_weights", "scope=? AND feature=?", arrayOf(scope, feature))
    }

    override fun weightsInScope(scope: String): Map<String, WeightRow> =
        query("SELECT feature, alpha, beta, weight FROM model_weights WHERE scope=?", arrayOf(scope)) {
            it.getString(0) to WeightRow(it.getDouble(1), it.getDouble(2), it.getDouble(3))
        }.toMap()

    override fun weightCount(): Int = query("SELECT COUNT(*) FROM model_weights") { it.getInt(0) }.first()

    override fun latestCalibration(): Calibration.Calib? =
        query("SELECT payload FROM calibration ORDER BY updated_at DESC, id DESC LIMIT 1") {
            val o = json.parseToJsonElement(it.str(0)) as JsonObject
            Calibration.Calib(JsonCodec.str(o, "kind", "prior"), JsonCodec.dbl(o, "a") ?: 0.0, JsonCodec.dbl(o, "b") ?: 0.0, (JsonCodec.dbl(o, "n") ?: 0.0).toInt())
        }.firstOrNull()

    override fun saveCalibration(c: Calibration.Calib, updatedAt: Double) {
        db.insert("calibration", null, ContentValues().apply {
            put("kind", c.kind)
            put("payload", """{"kind":"${c.kind}","a":${c.a},"b":${c.b},"n":${c.n}}""")
            put("updated_at", updatedAt)
        })
    }

    override fun addRating(row: RatingRow): Long =
        db.insert("ratings", null, ContentValues().apply {
            put("artist", row.artist); put("title", row.title); put("key", row.key); put("rating", row.rating)
            put("lane", row.lane); put("cluster", row.cluster)
            if (row.predictedConfidence != null) put("predicted_confidence", row.predictedConfidence) else putNull("predicted_confidence")
            put("created_at", row.createdAt)
        })

    override fun deleteRating(id: Long) { db.delete("ratings", "id=?", arrayOf(id.toString())) }

    override fun ratings(limit: Int): List<RatingRow> = query(
        "SELECT artist, title, key, rating, lane, cluster, predicted_confidence, created_at FROM ratings ORDER BY id DESC LIMIT $limit",
    ) { RatingRow(it.str(0), it.str(1), it.str(2), it.str(3), it.str(4), it.str(5), it.dblOrNull(6), it.getDouble(7)) }

    override fun ratingExists(key: String, createdAt: Double): Boolean =
        query("SELECT 1 FROM ratings WHERE key=? AND abs(created_at - ?) < 1e-6 LIMIT 1", arrayOf(key, createdAt.toString())) { 1 }.isNotEmpty()

    override fun addFeedback(key: String, event: String, artist: String, title: String, detail: String, lane: String, createdAt: Double): Long =
        db.insert("feedback_events", null, ContentValues().apply {
            put("key", key); put("event", event); put("artist", artist); put("title", title)
            put("detail", detail); put("lane", lane); put("created_at", createdAt)
        })

    override fun deleteFeedback(id: Long) { db.delete("feedback_events", "id=?", arrayOf(id.toString())) }

    override fun feedbackRows(event: String): List<FeedbackRow> = query(
        "SELECT id, key, IFNULL(artist,''), IFNULL(title,''), IFNULL(detail,''), IFNULL(lane,''), created_at FROM feedback_events WHERE event=? ORDER BY id",
        arrayOf(event),
    ) { FeedbackRow(it.getLong(0), it.getString(1), event, it.getString(2), it.getString(3), it.getString(4), it.getString(5), it.getDouble(6)) }

    override fun metaWithPrefix(prefix: String): Map<String, String> = query(
        "SELECT k, v FROM meta WHERE substr(k, 1, ?)=?", arrayOf(prefix.length.toString(), prefix),
    ) { it.getString(0) to it.getString(1) }.toMap()

    override fun seedArtistCounts(): Map<String, Int> = query(
        "SELECT artist, COUNT(*) c FROM seeds WHERE artist!='' AND artist!='Unknown' AND holdout=0 GROUP BY artist ORDER BY c DESC",
    ) { it.getString(0) to it.getInt(1) }.toMap()

    override fun likedSeeds(limit: Int): List<SeedRow> = query(
        "SELECT artist, title, key, source, IFNULL(video_id,''), holdout FROM seeds WHERE source IN (${LIKED_SEED_SOURCES.joinToString(",") { "'$it'" }}) " +
            "AND artist!='' AND artist!='Unknown' ORDER BY id LIMIT $limit",
    ) { SeedRow(it.getString(0), it.getString(1), it.getString(2), it.getString(3), it.getString(4), it.getInt(5) != 0) }

    override fun logRecommendation(row: LogRow): Long =
        db.insert("recommendations_log", null, ContentValues().apply {
            put("artist", row.artist); put("title", row.title); put("key", row.key); put("lane", row.lane)
            if (row.confidence != null) put("confidence", row.confidence) else putNull("confidence")
            put("why", row.why); put("why_json", row.whyJson); put("metrics_snapshot", row.metricsSnapshot)
            put("created_at", row.createdAt)
        })

    override fun deleteLog(id: Long) { db.delete("recommendations_log", "id=?", arrayOf(id.toString())) }

    override fun lastLog(key: String): LogRow? = query(
        "SELECT artist, title, key, lane, confidence, why, why_json, metrics_snapshot, created_at " +
            "FROM recommendations_log WHERE key=? ORDER BY id DESC LIMIT 1",
        arrayOf(key),
    ) { LogRow(it.str(0), it.str(1), it.str(2), it.str(3), it.dblOrNull(4), it.str(5), it.str(6), it.str(7), it.dblOrNull(8) ?: 0.0) }.firstOrNull()

    override fun logCount(): Int = query("SELECT COUNT(*) FROM recommendations_log") { it.getInt(0) }.first()

    override fun getTag(tag: String, kind: String): TagRow? = query(
        "SELECT tag, kind, count, as_dial, first_seen, last_seen FROM tag_vocabulary WHERE tag=? AND kind=?",
        arrayOf(tag, kind),
    ) { TagRow(it.str(0), it.str(1), it.getInt(2), it.getInt(3) != 0, it.dblOrNull(4), it.dblOrNull(5)) }.firstOrNull()

    override fun putTag(row: TagRow) {
        db.insertWithOnConflict("tag_vocabulary", null, ContentValues().apply {
            put("tag", row.tag); put("kind", row.kind); put("count", row.count); put("as_dial", if (row.asDial) 1 else 0)
            put("first_seen", row.firstSeen); put("last_seen", row.lastSeen)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun deleteTag(tag: String, kind: String) {
        db.delete("tag_vocabulary", "tag=? AND kind=?", arrayOf(tag, kind))
    }

    override fun listTags(kind: String?, dialsOnly: Boolean, limit: Int): List<TagRow> {
        val sb = StringBuilder("SELECT tag, kind, count, as_dial, first_seen, last_seen FROM tag_vocabulary WHERE 1=1")
        val args = ArrayList<String>()
        if (kind != null) { sb.append(" AND kind=?"); args += kind }
        if (dialsOnly) sb.append(" AND as_dial=1")
        sb.append(" ORDER BY count DESC, tag ASC LIMIT $limit")
        return query(sb.toString(), args.toTypedArray()) {
            TagRow(it.str(0), it.str(1), it.getInt(2), it.getInt(3) != 0, it.dblOrNull(4), it.dblOrNull(5))
        }
    }

    override fun resolveCacheOk(key: String): ResolveRow? = query(
        "SELECT key, artist, title, video_id, score, matched_name, status, source, raw_error, updated_at FROM yt_play_resolve " +
            "WHERE key=? AND status='ok' AND video_id!='' AND IFNULL(source,'') NOT IN ('user_correction','user_correction_override')",
        arrayOf(key),
    ) { ResolveRow(it.str(0), it.str(1), it.str(2), it.str(3), it.dblOrNull(4), it.str(5), it.str(6), it.str(7), it.str(8), it.dblOrNull(9) ?: 0.0) }
        .firstOrNull()

    override fun putResolve(row: ResolveRow) {
        db.insertWithOnConflict("yt_play_resolve", null, ContentValues().apply {
            put("key", row.key); put("artist", row.artist); put("title", row.title); put("video_id", row.videoId)
            put("score", row.score); put("matched_name", row.matched); put("status", row.status); put("source", row.source)
            put("raw_error", row.error); put("updated_at", row.updatedAt)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun correction(key: String): String? =
        query("SELECT video_id FROM yt_play_corrections WHERE key=?", arrayOf(key)) { it.str(0) }.firstOrNull()?.ifEmpty { null }

    override fun putCorrection(key: String, artist: String, title: String, videoId: String, note: String, updatedAt: Double) {
        db.insertWithOnConflict("yt_play_corrections", null, ContentValues().apply {
            put("key", key); put("artist", artist); put("title", title); put("video_id", videoId)
            put("note", note); put("updated_at", updatedAt)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun httpCacheGet(key: String, now: Double): String? = query(
        "SELECT payload, created_at, ttl_seconds FROM cache WHERE cache_key=?", arrayOf(key),
    ) { if (now - it.getDouble(1) > it.getLong(2)) null else it.str(0) }.firstOrNull()

    override fun httpCacheSet(key: String, payload: String, ttlSeconds: Long, now: Double) {
        db.insertWithOnConflict("cache", null, ContentValues().apply {
            put("cache_key", key); put("payload", payload); put("created_at", now); put("ttl_seconds", ttlSeconds)
        }, SQLiteDatabase.CONFLICT_REPLACE)
    }

    override fun meta(key: String): String? = query("SELECT v FROM meta WHERE k=?", arrayOf(key)) { it.str(0) }.firstOrNull()

    override fun setMeta(key: String, value: String) {
        db.insertWithOnConflict("meta", null, ContentValues().apply { put("k", key); put("v", value) }, SQLiteDatabase.CONFLICT_REPLACE)
    }
}
