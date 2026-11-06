package com.brian.musicrec.core.engine

import kotlin.math.abs

/** Port of `dials/dials.py::DialSet`. */
data class DialSet(
    val filters: Map<String, Pair<Double, Double>> = emptyMap(),
    /** name -> (desired, weight) */
    val targets: Map<String, Pair<Double, Double>> = emptyMap(),
    val ignore: Set<String> = emptySet(),
    val lane: String = "auto",
    val preset: String = "",
    /** null = all chips on (unconstrained). Empty = none on. Else OR-include allow-list. */
    val enabledGenres: Set<String>? = null,
) {
    fun withLane(l: String): DialSet = copy(lane = l.ifEmpty { lane })

    fun enabledGenresKey(): List<String>? = enabledGenres?.sorted()

    fun passesFilters(metrics: Map<String, MetricValue>): Pair<Boolean, List<String>> {
        val fails = ArrayList<String>()
        for ((name, range) in filters) {
            if (name in ignore) continue
            val m = metrics[name]
            val preferRaw = name.endsWith("_days") || name in RAW_FILTERS
            var v = if (preferRaw) m?.raw else m?.norm
            if (v == null) v = m?.raw
            if (v == null) {
                fails += "$name=missing"; continue
            }
            if (!(range.first <= v && v <= range.second)) fails += "$name=$v not in [${range.first},${range.second}]"
        }
        return (fails.isEmpty()) to fails
    }

    fun targetScore(metrics: Map<String, MetricValue>): Double {
        if (targets.isEmpty()) return 0.0
        var score = 0.0
        var wsum = 0.0
        for ((name, t) in targets) {
            val (desired, weight) = t
            if (name in ignore || weight <= 0) continue
            val m = metrics[name]
            var v = m?.norm
            val desiredN: Double
            if (v == null && m?.raw != null && name in EngineConfig.METRIC_DEFS) {
                v = Metrics.normalize(name, m.raw)
                desiredN = if (desired > 1 || name == "tempo_bpm") Metrics.normalize(name, desired)!! else desired
            } else {
                desiredN = desired
            }
            if (v == null) continue
            val closeness = maxOf(0.0, 1.0 - abs(v - desiredN))
            score += closeness * weight
            wsum += weight
        }
        return if (wsum != 0.0) score / wsum else 0.0
    }

    companion object {
        private val RAW_FILTERS = setOf(
            "tempo_bpm", "loudness", "brightness", "duration_sec", "era_year", "plays", "source_agreement", "key",
        )
    }
}

/** A dial preset (from presets.json). */
data class Preset(
    val description: String = "",
    val filters: Map<String, Pair<Double, Double>> = emptyMap(),
    val targets: Map<String, Pair<Double, Double>> = emptyMap(),
    val lane: String = "",
)

fun DialSet.applyPreset(name: String, presets: Map<String, Preset>): DialSet {
    val p = presets[name] ?: throw IllegalArgumentException("Unknown preset: $name")
    return copy(
        filters = filters + p.filters,
        targets = targets + p.targets,
        lane = p.lane.ifEmpty { lane },
        preset = name,
    )
}
