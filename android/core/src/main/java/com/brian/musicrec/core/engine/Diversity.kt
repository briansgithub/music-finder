package com.brian.musicrec.core.engine

/**
 * Batch-2 card picker (parity with `engine/diversity.py`):
 *  - lane choice: in auto, fresh is a 25 % *cap* (never a quota) and a fresh card
 *    is offered only when the best fresh affinity is within 0.15 of the best proven;
 *    lane=fresh reports exhaustion instead of silently serving proven.
 *  - every 5th card is an exploration slot: the least-known candidate (lowest Σ N
 *    over its features) among the top 30.
 *  - otherwise MMR over the top 30: selection − λ·max-similarity to the last 10
 *    cards (λ = 0.1), with at most one card per artist per 5 cards; if nothing
 *    passes, artist spacing is relaxed first.
 */
object Diversity {
    const val FRESH_CAP = 0.25
    const val FRESH_GAP = 0.15
    const val LAMBDA = 0.1
    const val WINDOW = 10
    const val ARTIST_SPACING = 5
    const val EXPLORE_EVERY = 5
    const val TOP_K = 30

    data class Served(val key: String, val artistKey: String, val lane: String, val vec: Map<String, Double>)

    data class Pick(val cand: Candidate, val slot: String, val mmrPenalty: Double, val relaxed: Boolean)

    fun vector(c: Candidate): Map<String, Double> {
        val v = LinkedHashMap<String, Double>()
        for (f in Affinity.features(c)) v[f] = if (f.startsWith("artist:")) 1.0 else 0.5
        return v
    }

    fun served(c: Candidate) = Served(c.key(), Affinity.artistKey(c.artist), c.lane, vector(c))

    fun similarity(a: Map<String, Double>, b: Map<String, Double>): Double {
        var num = 0.0
        var den = 0.0
        val keys = LinkedHashSet<String>().apply { addAll(a.keys); addAll(b.keys) }
        for (k in keys) {
            val x = a[k] ?: 0.0
            val y = b[k] ?: 0.0
            num += minOf(x, y)
            den += maxOf(x, y)
        }
        return if (den > 0) num / den else 0.0
    }

    /** "fresh" | "proven" | null (nothing available / fresh exhausted in fresh mode). */
    fun chooseLane(pref: String, proven: List<Candidate>, fresh: List<Candidate>, history: List<Served>): String? = when (pref) {
        "fresh" -> if (fresh.isNotEmpty()) "fresh" else null
        "proven" -> if (proven.isNotEmpty()) "proven" else null
        else -> {
            if (fresh.isEmpty()) {
                if (proven.isNotEmpty()) "proven" else null
            } else if (proven.isEmpty()) {
                "fresh"
            } else {
                val nFresh = history.count { it.lane == "fresh" }
                val shareAfter = (nFresh + 1).toDouble() / (history.size + 1)
                val bestF = fresh.maxOf { it.affinity }
                val bestP = proven.maxOf { it.affinity }
                if (shareAfter <= FRESH_CAP && bestF >= bestP - FRESH_GAP) "fresh" else "proven"
            }
        }
    }

    fun pick(pool: List<Candidate>, history: List<Served>, stats: Affinity.Stats): Pick? {
        if (pool.isEmpty()) return null
        val top = pool.take(TOP_K)
        val slotNo = history.size + 1
        val recentArtists = history.takeLast(ARTIST_SPACING - 1).map { it.artistKey }.toSet()
        val window = history.takeLast(WINDOW)
        fun spaced(c: Candidate) = Affinity.artistKey(c.artist) !in recentArtists
        fun penalty(c: Candidate): Double {
            val v = vector(c)
            return LAMBDA * (window.maxOfOrNull { similarity(v, it.vec) } ?: 0.0)
        }
        val exploration = slotNo % EXPLORE_EVERY == 0
        for (relax in listOf(false, true)) {
            val eligible = if (relax) top else top.filter { spaced(it) }
            if (eligible.isEmpty()) continue
            if (exploration) {
                var best: Candidate? = null
                var bestK = Double.MAX_VALUE
                for (c in eligible) {
                    val k = stats.knownness(Affinity.features(c))
                    if (k < bestK) { bestK = k; best = c }
                }
                return Pick(best!!, "exploration", penalty(best), relax)
            }
            var best: Candidate? = null
            var bestV = -Double.MAX_VALUE
            var bestPen = 0.0
            for (c in eligible) {
                val pen = penalty(c)
                val v = c.selection - pen
                if (v > bestV) { bestV = v; best = c; bestPen = pen }
            }
            return Pick(best!!, "mmr", bestPen, relax)
        }
        return null
    }
}
