package com.brian.musicrec.core.engine

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject

/**
 * Loads data into the on-device store from either:
 *  - a seed bundle (`"format": "musicfinder-seed"`) produced on the PC by
 *    `recommender/scripts/export_android_seed.py` (also shipped in app assets), or
 *  - the PC engine's `GET /export` JSON (ratings, dials, tags).
 *
 * Cleaning applied on the way in (same rules as the engine): seed hard-drop
 * title filters, artist skip list, implausible/playlist-shaped artists, and the
 * seed/rated/already-recommended blocklist.
 */
class SeedImporter(private val store: EngineStore) {
    data class Summary(
        val kind: String,
        val candidates: Int = 0,
        val droppedCandidates: Int = 0,
        val seeds: Int = 0,
        val ratingsAdded: Int = 0,
        val tags: Int = 0,
        val weights: Int = 0,
        val resolves: Int = 0,
        val corrections: Int = 0,
        val logRows: Int = 0,
        val newRatings: List<RatingRow> = emptyList(),
        val enabledGenres: List<String>? = null,
        val enabledGenresPresent: Boolean = false,
        val negatives: Int = 0,
    ) {
        override fun toString(): String =
            "$kind: candidates=$candidates (dropped $droppedCandidates), seeds=$seeds, ratings+=$ratingsAdded, " +
                "tags=$tags, weights=$weights, video_ids=$resolves, no_video=$negatives, corrections=$corrections, log=$logRows"
    }

    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    fun importText(text: String, now: Double): Summary {
        val root = json.parseToJsonElement(text).jsonObject
        return if (JsonCodec.str(root, "format") == "musicfinder-seed") importBundle(root, now) else importExport(root, now)
    }

    private fun arr(o: JsonObject, k: String): List<JsonObject> =
        (o[k] as? JsonArray)?.mapNotNull { it as? JsonObject } ?: emptyList()

    fun importBundle(root: JsonObject, now: Double): Summary = store.tx {
        val skip = (root["skip_list"] as? JsonObject)?.let {
            JsonCodec.strList(it["artists"]) + JsonCodec.strList(it["channels"])
        } ?: emptyList()
        store.setMeta("skip_list", Json.encodeToString(JsonElement.serializer(), JsonArray(skip.map { JsonPrimitive(it) })))
        (root["presets"] as? JsonObject)?.let { store.setMeta("presets", it.toString()) }
        store.setMeta("seed_artists", (root["seed_artists"] ?: JsonArray(emptyList())).toString())

        var seeds = 0
        if (store.meta("seed_version") == null) {
            for (s in arr(root, "seeds")) {
                store.putSeed(
                    SeedRow(
                        JsonCodec.str(s, "artist"), JsonCodec.str(s, "title"), JsonCodec.str(s, "key"),
                        JsonCodec.str(s, "source"), JsonCodec.str(s, "video_id"),
                        (JsonCodec.dbl(s, "holdout") ?: 0.0) != 0.0,
                    ),
                )
                seeds++
            }
        }
        JsonCodec.strList(root["blocked_keys"]).forEach { store.putBlockedKey(it) }
        JsonCodec.strList(root["blocked_video_ids"]).forEach { store.putBlockedVideoId(it) }

        // Ratings / log first so the blocklist is complete before cleaning the pool.
        val newRatings = mergeRatings(arr(root, "ratings"))
        var logRows = 0
        val logUntil = store.meta("log_imported_until")?.toDoubleOrNull() ?: 0.0
        var maxLog = logUntil
        for (r in arr(root, "recommendations_log")) {
            val ts = JsonCodec.dbl(r, "created_at") ?: 0.0
            if (ts <= logUntil) continue
            store.logRecommendation(
                LogRow(
                    JsonCodec.str(r, "artist"), JsonCodec.str(r, "title"), JsonCodec.str(r, "key"),
                    JsonCodec.str(r, "lane"), JsonCodec.dbl(r, "confidence"), JsonCodec.str(r, "why"),
                    JsonCodec.str(r, "why_json", "[]"), JsonCodec.str(r, "metrics_snapshot", "{}"), ts,
                ),
            )
            maxLog = maxOf(maxLog, ts); logRows++
        }
        store.setMeta("log_imported_until", maxLog.toString())
        for (f in arr(root, "feedback_events")) {
            val ts = JsonCodec.dbl(f, "created_at") ?: continue
            if (ts <= logUntil) continue
            store.addFeedback(
                JsonCodec.str(f, "key"), JsonCodec.str(f, "event"), JsonCodec.str(f, "artist"), JsonCodec.str(f, "title"),
                JsonCodec.str(f, "detail"), JsonCodec.str(f, "lane"), ts,
            )
        }

        // Candidate pool (replaced on every bundle import), cleaned.
        val blocked = store.blockedKeys()
        val blockedVids = store.blockedVideoIds()
        store.clearCandidates()
        var kept = 0
        var dropped = 0
        val cands = root["candidates"] as? JsonObject
        for (pool in listOf("proven", "fresh")) {
            for (o in (cands?.get(pool) as? JsonArray)?.mapNotNull { it as? JsonObject } ?: emptyList()) {
                val c = JsonCodec.candidate(o)
                if (!cleanCandidate(c, skip, blocked, blockedVids)) { dropped++; continue }
                store.putCandidate(pool, c); kept++
            }
        }
        (root["track_metrics"] as? JsonObject)?.forEach { (k, v) -> store.putMetrics(k, JsonCodec.metrics(v)) }

        var weights = 0
        for (w in arr(root, "model_weights")) {
            store.insertWeightIfAbsent(
                JsonCodec.str(w, "scope"), JsonCodec.str(w, "feature"),
                WeightRow(JsonCodec.dbl(w, "alpha") ?: 1.0, JsonCodec.dbl(w, "beta") ?: 1.0, JsonCodec.dbl(w, "weight") ?: 0.0),
                JsonCodec.dbl(w, "updated_at") ?: now,
            )
            weights++
        }
        (root["calibration"] as? JsonObject)?.let { c ->
            if (store.latestCalibration() == null) {
                store.saveCalibration(
                    Calibration.Calib(JsonCodec.str(c, "kind", "prior"), JsonCodec.dbl(c, "a") ?: 0.0, JsonCodec.dbl(c, "b") ?: 0.0, (JsonCodec.dbl(c, "n") ?: 0.0).toInt()),
                    now,
                )
            }
        }
        val tags = mergeTags(arr(root, "tag_vocabulary"), now)
        var resolves = 0
        for (r in arr(root, "yt_play_resolve")) {
            val vid = JsonCodec.str(r, "video_id")
            if (vid.length != 11) continue
            store.putResolve(
                ResolveRow(
                    JsonCodec.str(r, "key"), JsonCodec.str(r, "artist"), JsonCodec.str(r, "title"), vid,
                    JsonCodec.dbl(r, "score"), JsonCodec.str(r, "matched_name"), "ok",
                    JsonCodec.str(r, "source", "pc"), null, JsonCodec.dbl(r, "updated_at") ?: now,
                ),
            )
            resolves++
        }
        // Definitive "no video" results from the box pre-resolve: don't look these up again on the phone.
        var negatives = 0
        for (r in arr(root, "yt_play_resolve_negative")) {
            val key = JsonCodec.str(r, "key")
            if (key.isEmpty() || store.resolveCacheOk(key) != null) continue
            val at = JsonCodec.dbl(r, "updated_at") ?: now
            val ttl = maxOf(7L * 24 * 3600, VideoResolver.NEGATIVE_TTL_SEC - (now - at).toLong())
            store.httpCacheSet(VideoResolver.NEG_PREFIX + key, "{\"status\":\"${JsonCodec.str(r, "status", "no_match")}\",\"from\":\"seed\"}", ttl, now)
            negatives++
        }
        var corrections = 0
        for (r in arr(root, "yt_play_corrections")) {
            val key = JsonCodec.str(r, "key")
            if (store.correction(key) != null) continue
            store.putCorrection(key, JsonCodec.str(r, "artist"), JsonCodec.str(r, "title"), JsonCodec.str(r, "video_id"), JsonCodec.str(r, "note"), JsonCodec.dbl(r, "updated_at") ?: now)
            corrections++
        }
        store.setMeta("seed_version", "${JsonCodec.str(root, "engine_version")}@${JsonCodec.dbl(root, "exported_at") ?: 0.0}")
        store.setMeta("seed_imported_at", now.toString())
        (root["exported_at"] as? JsonPrimitive)?.contentOrNull?.let { store.setMeta("seed_exported_raw", it) }
        Summary("seed", kept, dropped, seeds, newRatings.size, tags, weights, resolves, corrections, logRows, newRatings, negatives = negatives)
    }

    /** PC `GET /export`: {exported_at, ratings[], dials{enabled_genres…}, tags[]}. */
    fun importExport(root: JsonObject, now: Double): Summary = store.tx {
        if (root["ratings"] == null && root["tags"] == null) {
            throw IllegalArgumentException("Not a Music Finder seed bundle or /export JSON")
        }
        val newRatings = mergeRatings(arr(root, "ratings"))
        val tags = mergeTags(arr(root, "tags"), now)
        val dials = root["dials"] as? JsonObject
        val present = dials != null && dials.containsKey("enabled_genres")
        val eg = dials?.get("enabled_genres")
        val enabled = if (eg is JsonArray) JsonCodec.strList(eg) else null
        Summary("export", ratingsAdded = newRatings.size, tags = tags, newRatings = newRatings, enabledGenres = enabled, enabledGenresPresent = present)
    }

    private fun mergeRatings(rows: List<JsonObject>): List<RatingRow> {
        val added = ArrayList<RatingRow>()
        for (r in rows.sortedBy { JsonCodec.dbl(it, "created_at") ?: 0.0 }) {
            val artist = JsonCodec.str(r, "artist")
            val title = JsonCodec.str(r, "title")
            val rating = JsonCodec.str(r, "rating")
            if (artist.isEmpty() || title.isEmpty() || rating !in setOf("love", "like", "meh", "skip", "dislike")) continue
            val key = JsonCodec.str(r, "key").ifEmpty { normalizeKey(artist, title) }
            val ts = JsonCodec.dbl(r, "created_at") ?: continue
            if (store.ratingExists(key, ts)) continue
            val row = RatingRow(artist, title, key, rating, JsonCodec.str(r, "lane"), JsonCodec.str(r, "cluster"), JsonCodec.dbl(r, "predicted_confidence"), ts)
            store.addRating(row)
            added += row
        }
        return added
    }

    private fun mergeTags(rows: List<JsonObject>, now: Double): Int {
        var n = 0
        for (t in rows) {
            val tag = JsonCodec.str(t, "tag")
            val kind = JsonCodec.str(t, "kind", "tag")
            if (tag.isEmpty()) continue
            val count = (JsonCodec.dbl(t, "count") ?: 0.0).toInt()
            val asDial = (JsonCodec.dbl(t, "as_dial") ?: 0.0) != 0.0
            val prev = store.getTag(tag, kind)
            if (prev == null || prev.count < count) {
                store.putTag(TagRow(tag, kind, count, asDial || (prev?.asDial ?: false), JsonCodec.dbl(t, "first_seen") ?: now, JsonCodec.dbl(t, "last_seen") ?: now))
                n++
            }
        }
        return n
    }

    companion object {
        fun cleanCandidate(c: Candidate, skip: Collection<String>, blocked: Set<String>, blockedVids: Set<String>): Boolean {
            if (Originality.isBlocked(c, blocked, blockedVids)) return false
            if (TitleFilters.classify(c.title, c.artist).action == "drop") return false
            if (TitleFilters.artistSkipped(c.artist, skip)) return false
            if (!Sanitize.isPlausibleArtist(c.artist)) return false
            return true
        }

        fun skipList(store: EngineStore): List<String> = runCatching {
            JsonCodec.strList(Json.parseToJsonElement(store.meta("skip_list") ?: "[]"))
        }.getOrDefault(emptyList())

        fun presets(store: EngineStore): Map<String, Preset> = runCatching {
            val o = Json.parseToJsonElement(store.meta("presets") ?: "{}") as JsonObject
            o.mapValues { JsonCodec.preset(it.value as JsonObject) }
        }.getOrDefault(emptyMap())
    }
}

@Suppress("unused")
private fun JsonElement.asStringOrNull(): String? = (this as? JsonPrimitive)?.contentOrNull
