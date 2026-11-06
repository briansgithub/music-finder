package com.brian.musicrec.core.engine

/** Port of `musicrec.config` constants used by ranking. */
object EngineConfig {
    const val VERSION = "1.3.0-android"
    const val PROVEN_MIN_CONFIDENCE = 0.45
    const val PROVEN_MIN_SOURCES = 2
    const val FRESH_DAYS = 30
    /** Fresh lane serves only tracks at/above this confidence (tunable in Settings); below → proven. */
    const val FRESH_MIN_CONFIDENCE = 0.7
    /** Dislike = strong negative (mirror of love) + soft per-artist down-weight, never a ban. */
    const val DISLIKE_STRENGTH = 1.25
    const val ARTIST_DISLIKE_STEP = 0.3
    const val ARTIST_RECOVER_STEP = 0.15
    const val ARTIST_MIN_WEIGHT = -0.6
    const val ARTIST_PENALTY_SCALE = 2.0
    const val EXPLORATION_RATE = 0.10
    const val MAX_PER_ARTIST_BATCH = 1
    const val MAINSTREAM_FAN_THRESHOLD = 500_000.0
    const val TAG_DIAL_THRESHOLD = 5

    val DEFAULT_CLUSTERS = listOf(
        "classical", "ragtime", "soundtrack", "game", "electronic",
        "metal", "pop", "jazz", "piano", "ambient", "folk", "vocal",
        "trance", "instrumental", "other",
    )

    data class MetricDef(val kind: String, val unit: String, val lo: Double, val hi: Double)

    val METRIC_DEFS: Map<String, MetricDef> = linkedMapOf(
        "tempo_bpm" to MetricDef("audio", "bpm", 40.0, 200.0),
        "energy" to MetricDef("audio", "0-1", 0.0, 1.0),
        "loudness" to MetricDef("audio", "dBFS", -60.0, 0.0),
        "brightness" to MetricDef("audio", "Hz", 500.0, 6000.0),
        "percussiveness" to MetricDef("audio", "0-1", 0.0, 1.0),
        "danceability" to MetricDef("audio", "0-1", 0.0, 1.0),
        "acousticness" to MetricDef("audio", "0-1", 0.0, 1.0),
        "instrumentalness" to MetricDef("audio", "0-1", 0.0, 1.0),
        "key" to MetricDef("audio", "pitch_class", 0.0, 11.0),
        "mode" to MetricDef("audio", "0=minor,1=major", 0.0, 1.0),
        "dynamic_range" to MetricDef("audio", "dB", 0.0, 40.0),
        "duration_sec" to MetricDef("audio", "sec", 30.0, 600.0),
        "freshness_days" to MetricDef("context", "days", 0.0, 3650.0),
        "obscurity" to MetricDef("context", "0-1", 0.0, 1.0),
        "era_year" to MetricDef("context", "year", 1950.0, 2030.0),
        "source_agreement" to MetricDef("context", "count", 0.0, 5.0),
        "plays" to MetricDef("personal", "count", 0.0, 100.0),
        "rating_value" to MetricDef("personal", "0-1", 0.0, 1.0),
        "implicit_score" to MetricDef("personal", "0-1", 0.0, 1.0),
        "like_probability" to MetricDef("predicted", "0-1", 0.0, 1.0),
    )
}

/** One metric value (raw + 0..1 norm), as in `metrics.layer.build_metric_dict`. */
data class MetricValue(
    val raw: Double?,
    val norm: Double?,
    val unit: String = "",
    val kind: String = "",
)

object Metrics {
    fun normalize(name: String, raw: Double?): Double? {
        if (raw == null) return null
        val meta = EngineConfig.METRIC_DEFS[name] ?: return clamp01(raw)
        if (meta.hi == meta.lo) return 0.5
        return clamp01((raw - meta.lo) / (meta.hi - meta.lo))
    }

    private fun clamp01(v: Double) = maxOf(0.0, minOf(1.0, v))

    fun build(rawMap: Map<String, Double?>): LinkedHashMap<String, MetricValue> {
        val out = LinkedHashMap<String, MetricValue>()
        for ((name, raw) in rawMap) {
            val meta = EngineConfig.METRIC_DEFS[name]
            out[name] = MetricValue(
                raw = raw,
                norm = normalize(name, raw),
                unit = meta?.unit ?: "",
                kind = meta?.kind ?: "other",
            )
        }
        return out
    }
}

/** Port of `models.Candidate`. Mutable like the Python dataclass. */
data class Candidate(
    var artist: String,
    var title: String,
    var album: String = "",
    var releaseDate: String = "",
    /** Insertion order matters (mirrors Python dict order in scoring/why). */
    val sourceSignals: LinkedHashMap<String, Double> = LinkedHashMap(),
    var tags: List<String> = emptyList(),
    var relatedToArtists: List<String> = emptyList(),
    var popularity: Double? = null,
    val externalIds: LinkedHashMap<String, String> = LinkedHashMap(),
    var previewUrl: String = "",
    var youtubeUrl: String = "",
    var lane: String = "",
    var confidence: Double = 0.0,
    var why: String = "",
    var whyParts: List<WhyPart> = emptyList(),
    var score: Double = 0.0,
    var cluster: String = "other",
    var metrics: LinkedHashMap<String, MetricValue> = LinkedHashMap(),
    /** Batch 2: personal affinity Z and the ranking score (Z + small legacy term). */
    var affinity: Double = 0.0,
    var selection: Double = 0.0,
) {
    fun key(): String = normalizeKey(artist, title)

    fun deepCopy(): Candidate = copy(
        sourceSignals = LinkedHashMap(sourceSignals),
        externalIds = LinkedHashMap(externalIds),
        tags = tags.toList(),
        relatedToArtists = relatedToArtists.toList(),
        metrics = LinkedHashMap(metrics),
        whyParts = whyParts.toList(),
    )
}

/** One "why this" contribution (feature, value, weight, contrib). */
data class WhyPart(val feature: String, val value: Double, val weight: Double, val contrib: Double)
