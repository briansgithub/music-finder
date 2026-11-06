package com.brian.musicrec.core.engine

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.put

/** JSON <-> engine models, using the Python engine's snake_case field names. */
object JsonCodec {
    fun str(o: JsonObject?, k: String, def: String = ""): String {
        val e = o?.get(k) ?: return def
        if (e is JsonNull) return def
        return (e as? JsonPrimitive)?.contentOrNull ?: def
    }

    fun dbl(o: JsonObject?, k: String): Double? {
        val e = o?.get(k) ?: return null
        if (e is JsonNull) return null
        val p = e as? JsonPrimitive ?: return null
        return p.doubleOrNull ?: p.booleanOrNull?.let { if (it) 1.0 else 0.0 }
    }

    fun strList(e: JsonElement?): List<String> =
        (e as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: emptyList()

    fun metric(e: JsonElement?): MetricValue? {
        val o = e as? JsonObject ?: return null
        return MetricValue(dbl(o, "raw"), dbl(o, "norm"), str(o, "unit"), str(o, "kind"))
    }

    fun metrics(e: JsonElement?): LinkedHashMap<String, MetricValue> {
        val out = LinkedHashMap<String, MetricValue>()
        (e as? JsonObject)?.forEach { (k, v) -> metric(v)?.let { out[k] = it } }
        return out
    }

    fun metricsJson(m: Map<String, MetricValue>): JsonObject = buildJsonObject {
        for ((k, v) in m) put(k, buildJsonObject {
            put("raw", v.raw); put("norm", v.norm); put("unit", v.unit); put("kind", v.kind)
        })
    }

    fun candidate(o: JsonObject): Candidate {
        val signals = LinkedHashMap<String, Double>()
        (o["source_signals"] as? JsonObject)?.forEach { (k, v) -> (v as? JsonPrimitive)?.doubleOrNull?.let { signals[k] = it } }
        val ext = LinkedHashMap<String, String>()
        (o["external_ids"] as? JsonObject)?.forEach { (k, v) -> (v as? JsonPrimitive)?.contentOrNull?.let { ext[k] = it } }
        return Candidate(
            artist = str(o, "artist"),
            title = str(o, "title"),
            album = str(o, "album"),
            releaseDate = str(o, "release_date"),
            sourceSignals = signals,
            tags = strList(o["tags"]),
            relatedToArtists = strList(o["related_to_artists"]),
            popularity = dbl(o, "popularity"),
            externalIds = ext,
            previewUrl = str(o, "preview_url"),
            youtubeUrl = str(o, "youtube_url"),
            lane = str(o, "lane"),
            confidence = dbl(o, "confidence") ?: 0.0,
            why = str(o, "why"),
            score = dbl(o, "score") ?: 0.0,
            cluster = str(o, "cluster", "other"),
            metrics = metrics(o["metrics"]),
        )
    }

    fun candidateJson(c: Candidate, full: Boolean = false): JsonObject = buildJsonObject {
        put("artist", c.artist); put("title", c.title); put("album", c.album)
        put("release_date", c.releaseDate)
        put("source_signals", buildJsonObject { c.sourceSignals.forEach { (k, v) -> put(k, v) } })
        put("tags", buildJsonArray { c.tags.forEach { add(JsonPrimitive(it)) } })
        put("related_to_artists", buildJsonArray { c.relatedToArtists.forEach { add(JsonPrimitive(it)) } })
        put("popularity", c.popularity)
        put("external_ids", buildJsonObject { c.externalIds.forEach { (k, v) -> put(k, v) } })
        put("preview_url", c.previewUrl); put("youtube_url", c.youtubeUrl)
        put("cluster", c.cluster)
        if (full) {
            put("lane", c.lane); put("confidence", c.confidence); put("score", c.score); put("why", c.why)
            put("why_parts", whyPartsJson(c.whyParts))
            put("metrics", metricsJson(c.metrics))
        }
    }

    fun whyPartsJson(parts: List<WhyPart>): JsonArray = buildJsonArray {
        parts.forEach { p ->
            add(buildJsonObject { put("feature", p.feature); put("value", p.value); put("weight", p.weight); put("contrib", p.contrib) })
        }
    }

    fun preset(o: JsonObject): Preset {
        fun pairs(e: JsonElement?, defSecond: Double?): Map<String, Pair<Double, Double>> {
            val out = LinkedHashMap<String, Pair<Double, Double>>()
            (e as? JsonObject)?.forEach { (k, v) ->
                val arr = (v as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.doubleOrNull } ?: return@forEach
                if (arr.isEmpty()) return@forEach
                val second = arr.getOrNull(1) ?: defSecond ?: arr[0]
                out[k] = arr[0] to second
            }
            return out
        }
        return Preset(
            description = str(o, "description"),
            filters = pairs(o["filters"], null),
            targets = pairs(o["targets"], 1.0),
            lane = str(o, "lane"),
        )
    }
}
