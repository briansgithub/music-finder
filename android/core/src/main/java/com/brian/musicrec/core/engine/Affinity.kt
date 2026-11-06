package com.brian.musicrec.core.engine

import kotlin.math.ln
import kotlin.math.tanh

/**
 * Batch-2 personal affinity (port-parity with `engine/affinity.py`). No AI/LLM.
 *
 *  - One active judgment per recording (the latest of love/like/meh/dislike/skip);
 *    utilities love 1, like 0.5, meh −0.25, dislike −1, skip none (counts as the
 *    active judgment but contributes nothing).
 *  - Shrinkage feature stats: per feature N (judged recordings having it) and S
 *    (sum of utilities); shrunk mean S/(N+κ), confidence N/(N+κ);
 *    κ tag 8, artist 5, label 20; channel features are off.
 *  - Family score = confidence-weighted average of shrunk means.
 *  - Seed similarity = weighted Jaccard (candidate support) between the candidate's
 *    artist vector (self 1.0, related seed artists 0.5) and the seed profile
 *    (seed count per artist, capped at 3 → [0,1]); IDF capped at 3.
 *  - affinity Z = 0.55 seed + 0.25 tags + 0.15 artist + 0.05 label.
 *  - selection = Z + 0.1·tanh(legacy_score/4)   (ranking; MMR works on this)
 *  - p_like = the calibrated confidence (unchanged).
 *
 * Stats are derived from the ratings table every pool build, so Undo (which
 * deletes the rating row) stays exact without any incremental bookkeeping.
 */
object Affinity {
    const val MODEL_VERSION = "affinity-1"
    val UTILITY: Map<String, Double?> = mapOf("love" to 1.0, "like" to 0.5, "meh" to -0.25, "dislike" to -1.0, "skip" to null)
    val KAPPA = mapOf("tag" to 8.0, "artist" to 5.0, "label" to 20.0)
    const val W_SEED = 0.55
    const val W_TAGS = 0.25
    const val W_ARTIST = 0.15
    const val W_LABEL = 0.05
    const val SEED_CAP = 3.0
    const val IDF_CAP = 3.0
    const val RELATED_WEIGHT = 0.5
    const val LEGACY_WEIGHT = 0.1
    const val MATCH_LABEL_MIN_RATINGS = 50

    fun artistKey(a: String) = Learning.artistKey(a)
    private fun tagKey(t: String) = Py.collapseWs(Py.casefold(t).trim())

    /** Ordered feature keys of a candidate: artist, tags (+cluster), label. Channel: off. */
    fun features(artist: String, tags: List<String>, cluster: String, label: String?): List<String> {
        val out = ArrayList<String>()
        val a = artistKey(artist)
        if (a.isNotEmpty()) out += "artist:$a"
        val tagList = ArrayList(tags)
        if (cluster.isNotEmpty() && cluster != "other") tagList += cluster
        for (t in tagList) {
            val k = tagKey(t)
            if (k.isEmpty()) continue
            val f = "tag:$k"
            if (f !in out) out += f
        }
        val l = tagKey(label ?: "")
        if (l.isNotEmpty()) out += "label:$l"
        return out
    }

    fun features(c: Candidate): List<String> = features(c.artist, c.tags, c.cluster, c.externalIds["label"])

    fun typeOf(f: String) = f.substringBefore(':')

    class Stats(val n: Map<String, Double>, val s: Map<String, Double>, val judged: Int) {
        fun knownness(feats: List<String>): Double = feats.sumOf { n[it] ?: 0.0 }
        fun family(type: String, feats: List<String>): Double {
            val k = KAPPA[type] ?: return 0.0
            var num = 0.0
            var den = 0.0
            for (f in feats) {
                if (typeOf(f) != type) continue
                val nf = n[f] ?: 0.0
                if (nf == 0.0) continue
                val w = nf / (nf + k)
                val m = (s[f] ?: 0.0) / (nf + k)
                num += w * m
                den += w
            }
            return if (den > 0) num / den else 0.0
        }
    }

    data class Judgment(val key: String, val rating: String, val features: List<String>)

    /** Active judgment per key = the latest row (input must be oldest → newest). */
    fun activeJudgments(rowsOldestFirst: List<Judgment>, utility: Map<String, Double?> = UTILITY): List<Judgment> {
        val latest = LinkedHashMap<String, Judgment>()
        for (j in rowsOldestFirst) {
            if (j.rating !in utility) continue
            latest.remove(j.key)
            latest[j.key] = j
        }
        return latest.values.toList()
    }

    fun stats(active: List<Judgment>, utility: Map<String, Double?> = UTILITY): Stats {
        val n = HashMap<String, Double>()
        val s = HashMap<String, Double>()
        var judged = 0
        for (j in active) {
            val u = utility[j.rating] ?: continue
            judged++
            for (f in j.features) {
                if (typeOf(f) == "channel") continue
                n[f] = (n[f] ?: 0.0) + 1.0
                s[f] = (s[f] ?: 0.0) + u
            }
        }
        return Stats(n, s, judged)
    }

    // ---- seed similarity ----
    fun seedProfile(seedArtistCounts: Map<String, Int>): Map<String, Double> {
        val counts = HashMap<String, Int>()
        for ((a, c) in seedArtistCounts) {
            val k = artistKey(a)
            if (k.isEmpty()) continue
            counts["artist:$k"] = (counts["artist:$k"] ?: 0) + c
        }
        return counts.mapValues { minOf(1.0, it.value / SEED_CAP) }
    }

    fun seedVector(c: Candidate): LinkedHashMap<String, Double> {
        val v = LinkedHashMap<String, Double>()
        val a = artistKey(c.artist)
        if (a.isNotEmpty()) v["artist:$a"] = 1.0
        for (r in c.relatedToArtists) {
            val k = artistKey(r)
            if (k.isEmpty()) continue
            v["artist:$k"] = maxOf(v["artist:$k"] ?: 0.0, RELATED_WEIGHT)
        }
        return v
    }

    /** df over the candidate vectors of the current pool → capped IDF. */
    fun idf(pool: List<Candidate>): Map<String, Double> {
        val df = HashMap<String, Int>()
        for (c in pool) for (f in seedVector(c).keys) df[f] = (df[f] ?: 0) + 1
        val nc = pool.size.toDouble()
        return df.mapValues { (_, d) -> minOf(IDF_CAP, ln((1.0 + nc) / (1.0 + d)) + 1.0) }
    }

    fun seedSimilarity(c: Candidate, profile: Map<String, Double>, idf: Map<String, Double>): Double {
        var num = 0.0
        var den = 0.0
        for ((f, cv) in seedVector(c)) {
            val w = idf[f] ?: IDF_CAP
            val pv = profile[f] ?: 0.0
            num += minOf(cv, pv) * w
            den += maxOf(cv, pv) * w
        }
        return if (den > 0) num / den else 0.0
    }

    class Model(val stats: Stats, val profile: Map<String, Double>, val idf: Map<String, Double>, val ratingsCount: Int)

    data class Parts(val seed: Double, val tags: Double, val artist: Double, val label: Double, val z: Double)

    fun parts(m: Model, c: Candidate): Parts {
        val f = features(c)
        val seed = seedSimilarity(c, m.profile, m.idf)
        val tg = m.stats.family("tag", f)
        val ar = m.stats.family("artist", f)
        val lb = m.stats.family("label", f)
        val z = W_SEED * seed + W_TAGS * tg + W_ARTIST * ar + W_LABEL * lb
        return Parts(seed, tg, ar, lb, z)
    }

    /** Sets c.affinity and c.selection (call after the legacy scorer filled c.score). */
    fun apply(m: Model, c: Candidate): Parts {
        val p = parts(m, c)
        c.affinity = p.z
        c.selection = p.z + LEGACY_WEIGHT * tanh(c.score / 4.0)
        return p
    }

    /** Apply to a scored lane pool and sort by (selection, p_like) desc — stable, like Python. */
    fun rank(m: Model, pool: List<Candidate>): List<Candidate> {
        for (c in pool) apply(m, c)
        return pool.sortedWith(compareByDescending<Candidate> { it.selection }.thenByDescending { it.confidence })
    }

    /** Card label instead of a % until there are enough ratings to trust p_like. */
    fun matchLabel(z: Double, ratingsCount: Int, pLike: Double, exploration: Boolean): String = when {
        exploration -> "Exploration pick"
        ratingsCount >= MATCH_LABEL_MIN_RATINGS -> "p(like) ${Math.round(pLike * 100)}%"
        z >= 0.30 -> "Strong match"
        z >= 0.15 -> "Good match"
        z >= 0.05 -> "Possible match"
        else -> "Stretch"
    }
}
