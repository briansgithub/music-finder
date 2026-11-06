package com.brian.musicrec.core

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/** HTTP client for local musicrec serve (default 10.0.2.2:8765 on emulator). */
class EngineClient(
    var baseUrl: String = "http://127.0.0.1:8765",
) {
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()
    private val json = "application/json; charset=utf-8".toMediaType()

    fun health(): JSONObject = get("/health")

    fun next(
        lane: String = "auto",
        enabledGenres: List<String>? = null,
    ): Recommendation {
        val params = mutableListOf("lane=${enc(lane)}")
        if (enabledGenres != null) {
            params += "enabled_genres=${enc(enabledGenres.joinToString(","))}"
        }
        val obj = get("/next?${params.joinToString("&")}").getJSONObject("recommendation")
        return parseRec(obj)
    }

    fun rate(
        artist: String,
        title: String,
        rating: String,
        lane: String = "",
        confidence: Double? = null,
        cluster: String = "",
    ): Int {
        val body = JSONObject()
            .put("artist", artist)
            .put("title", title)
            .put("rating", rating)
            .put("lane", lane)
        if (cluster.isNotBlank()) body.put("cluster", cluster)
        if (confidence != null) body.put("confidence", confidence)
        return post("/rate", body).optInt("undo_depth", -1)
    }

    fun notNow(artist: String, title: String, lane: String = ""): Int {
        val body = JSONObject()
            .put("artist", artist)
            .put("title", title)
            .put("lane", lane)
        return post("/not_now", body).optInt("undo_depth", -1)
    }

    /** POST /undo — returns the restored card + remaining depth, or null when nothing to undo (404). */
    fun undo(currentArtist: String?, currentTitle: String?): Pair<Recommendation, Int>? {
        val body = JSONObject()
        if (!currentArtist.isNullOrBlank() && !currentTitle.isNullOrBlank()) {
            body.put("current", JSONObject().put("artist", currentArtist).put("title", currentTitle))
        }
        return try {
            val o = post("/undo", body)
            parseRec(o.getJSONObject("recommendation")) to o.optInt("undo_depth", 0)
        } catch (_: NoRecommendationException) {
            null
        }
    }

    fun dials(): DialState {
        val o = get("/dials")
        return DialState(
            lane = o.optString("lane", "auto"),
            preset = o.optString("preset", ""),
            availableTagDials = stringList(o.optJSONArray("available_tag_dials")),
            presets = stringList(o.optJSONArray("presets")),
            availableGenres = stringList(o.optJSONArray("available_genres")),
            enabledGenres = if (o.isNull("enabled_genres")) null
            else stringList(o.optJSONArray("enabled_genres")),
        )
    }

    fun setDials(
        lane: String,
        preset: String = "",
        enabledGenres: List<String>? = null,
    ) {
        val body = JSONObject()
            .put("lane", lane)
            .put("preset", preset)
        if (enabledGenres == null) body.put("enabled_genres", JSONObject.NULL)
        else body.put("enabled_genres", JSONArray(enabledGenres))
        post("/dials", body)
    }

    fun exportRaw(): String = get("/export").toString(2)

    fun correctVideo(artist: String, title: String, videoId: String): String {
        val body = JSONObject().put("artist", artist).put("title", title).put("video_id", videoId)
        return post("/correct_video", body).optJSONObject("correction")?.optString("video_id").orEmpty()
    }

    /** Bundled sample when engine unreachable. */
    fun sampleFallback(): Recommendation = Recommendation(
        artist = "LukHash",
        title = "The Kingdom",
        lane = "proven",
        confidence = 0.65,
        why = "bundled sample — start local engine for live recs",
        ytmSearchUrl = YtmHandoff.searchUrl("LukHash", "The Kingdom"),
        ytmUrl = YtmHandoff.searchUrl("LukHash", "The Kingdom"),
        ytmHandoff = "search_fallback",
        tags = listOf("electronic", "game"),
    )

    private fun parseRec(o: JSONObject): Recommendation {
        return Recommendation(
            artist = o.optString("artist"),
            title = o.optString("title"),
            lane = o.optString("lane"),
            confidence = o.optDouble("confidence", 0.0),
            score = o.optDouble("score", 0.0),
            why = o.optString("why"),
            ytmUrl = o.optString("ytm_url"),
            ytmSearchUrl = o.optString("ytm_search_url"),
            ytmIntent = o.optString("ytm_intent"),
            ytmHandoff = o.optString("ytm_handoff"),
            videoId = o.optString("video_id"),
            tags = stringList(o.optJSONArray("tags")),
            playback = o.optString("playback", "handoff_youtube_music"),
            cluster = o.optString("cluster"),
        )
    }

    private fun stringList(arr: JSONArray?): List<String> {
        if (arr == null) return emptyList()
        return buildList {
            for (i in 0 until arr.length()) add(arr.getString(i))
        }
    }

    private fun enc(value: String): String =
        URLEncoder.encode(value, Charsets.UTF_8.name())

    private fun get(path: String): JSONObject {
        val req = Request.Builder().url(baseUrl.trimEnd('/') + path).get().build()
        http.newCall(req).execute().use { res ->
            val body = res.body?.string().orEmpty()
            if (res.code == 404) throw NoRecommendationException(body)
            if (!res.isSuccessful) error("HTTP ${res.code}: $body")
            return JSONObject(body)
        }
    }

    private fun post(path: String, jsonObj: JSONObject): JSONObject {
        val req = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .post(jsonObj.toString().toRequestBody(json))
            .build()
        http.newCall(req).execute().use { res ->
            val body = res.body?.string().orEmpty()
            if (res.code == 404) throw NoRecommendationException(body)
            if (!res.isSuccessful) error("HTTP ${res.code}: $body")
            return if (body.isBlank()) JSONObject() else JSONObject(body)
        }
    }
}
