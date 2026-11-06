package com.brian.musicrec.core

/** Recommendation payload from local engine (metadata-first). */
data class Recommendation(
    val artist: String = "",
    val title: String = "",
    val lane: String = "",
    val confidence: Double = 0.0,
    val score: Double = 0.0,
    val why: String = "",
    val ytmUrl: String = "",
    val ytmSearchUrl: String = "",
    val ytmIntent: String = "",
    val ytmHandoff: String = "",
    val videoId: String = "",
    val tags: List<String> = emptyList(),
    val playback: String = "handoff_youtube_music",
    val cluster: String = "",
    /** Where the video ID came from (seeds, cache, musicbrainz_url, user_correction…). */
    val resolveSource: String = "",
    /** Batch 2: personal affinity Z, ranking score, and p(like) (= confidence). */
    val affinity: Double = 0.0,
    val selection: Double = 0.0,
    /** Shown instead of a % until ≥ 50 ratings ("Strong match", "Exploration pick", …). */
    val matchLabel: String = "",
    /** "mmr" | "exploration" | "" */
    val slot: String = "",
    /** e.g. "fresh (90-day)" when the expanded fresh window served this card. */
    val laneLabel: String = "",
    /** Friends mode: broad appeal B (0..1), or -1 when not computed. */
    val broadAppeal: Double = -1.0,
)

data class DialState(
    val lane: String = "auto",
    val preset: String = "",
    val availableTagDials: List<String> = emptyList(),
    val presets: List<String> = emptyList(),
    val availableGenres: List<String> = emptyList(),
    /**
     * Allow-list of chips currently ON.
     * `null` = all chips on (unconstrained, same as a full set).
     * Empty list = every chip off.
     */
    val enabledGenres: List<String>? = null,
) {
    fun chipOn(genre: String): Boolean =
        enabledGenres == null || genre in enabledGenres
}

/** Engine has no remaining candidate after filters. */
class NoRecommendationException(message: String) : RuntimeException(message)
