package com.brian.musicrec.core.engine

import kotlin.math.ln
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.random.Random

/** Port of `engine/learning.py`: Beta posteriors per (scope, feature). No AI/LLM. */
class Learning(private val store: EngineStore, private val clock: () -> Double = { System.currentTimeMillis() / 1000.0 }) {
    companion object {
        val RATING_VALUE = mapOf("love" to 1.0, "like" to 0.7, "meh" to 0.3, "skip" to 0.0, "dislike" to 0.0)
        val RATINGS = setOf("love", "like", "meh", "skip", "dislike", "not_now")

        fun strengthFor(rating: String): Double = if (rating == "dislike") EngineConfig.DISLIKE_STRENGTH else 1.0

        /** `learning.artist_scope_key`: casefold + collapse whitespace. */
        fun artistKey(artist: String): String = Py.collapseWs(Py.casefold(artist).trim())
        val IMPLICIT_VALUE = mapOf("early_skip" to 0.15, "full_listen" to 0.75, "replay" to 0.9, "add_playlist" to 0.95)
        val DEFAULT_FEATURES = listOf(
            "deezer", "deezer_fresh", "deezer_chart",
            "listenbrainz", "musicbrainz", "musicbrainz_fresh",
            "lastfm", "youtube_newpipe", "youtube_newpipe_related",
            "bandcamp", "soundcloud", "watched_artist",
            "artist_overlap", "tag_overlap", "freshness",
            "obscurity", "adventurous",
            "m_tempo_bpm", "m_energy", "m_loudness", "m_brightness",
            "m_percussiveness", "m_danceability", "m_acousticness",
            "m_instrumentalness", "m_dynamic_range", "m_obscurity",
            "m_freshness_days",
        )
    }

    private var priorsEnsured = false

    fun ensurePriors() {
        if (priorsEnsured) return
        val now = clock()
        store.tx {
            for (feat in DEFAULT_FEATURES) {
                store.insertWeightIfAbsent("global", feat, WeightRow(2.0, 2.0, 0.0), now)
                for (lane in listOf("proven", "fresh")) store.insertWeightIfAbsent("lane:$lane", feat, WeightRow(1.5, 1.5, 0.0), now)
                for (c in EngineConfig.DEFAULT_CLUSTERS) store.insertWeightIfAbsent("cluster:$c", feat, WeightRow(1.2, 1.2, 0.0), now)
            }
        }
        priorsEnsured = true
    }

    fun updateFromRating(features: Map<String, Double>, rating: String, lane: String = "", cluster: String = "") {
        if (rating == "not_now") return
        ensurePriors()
        val success = RATING_VALUE[rating] ?: return
        val mult = strengthFor(rating)
        val now = clock()
        val scopes = buildList {
            add("global")
            if (lane.isNotEmpty()) add("lane:$lane")
            if (cluster.isNotEmpty()) add("cluster:$cluster")
        }
        store.tx {
            for ((feat, rawStrength) in features) {
                if (rawStrength <= 0) continue
                val strength = rawStrength * mult
                for (scope in scopes) {
                    val row = store.getWeight(scope, feat) ?: WeightRow(1.0, 1.0, 0.0)
                    val decay = 0.995
                    var alpha = 1.0 + (row.alpha - 1.0) * decay
                    var beta = 1.0 + (row.beta - 1.0) * decay
                    alpha += success * strength
                    beta += (1.0 - success) * strength
                    val mean = alpha / (alpha + beta)
                    val weight = 0.7 * row.weight + 0.3 * (mean - 0.5) * 2.0
                    store.putWeight(scope, feat, WeightRow(alpha, beta, weight), now)
                }
            }
        }
    }

    /** (scope, feature) rows [updateFromRating] would write — for exact undo snapshots. */
    fun affectedKeys(features: Map<String, Double>, rating: String, lane: String = "", cluster: String = ""): List<Pair<String, String>> {
        if (rating == "not_now" || RATING_VALUE[rating] == null) return emptyList()
        val scopes = buildList {
            add("global")
            if (lane.isNotEmpty()) add("lane:$lane")
            if (cluster.isNotEmpty()) add("cluster:$cluster")
        }
        return features.filter { it.value > 0 }.keys.flatMap { f -> scopes.map { it to f } }
    }

    /** Rows [updateArtistAffinity] may write — for exact undo snapshots. */
    fun artistAffinityKeys(artist: String, rating: String): List<Pair<String, String>> {
        val a = artistKey(artist)
        return if (a.isNotEmpty() && rating in setOf("dislike", "love", "like")) listOf("artist" to a) else emptyList()
    }

    /**
     * `learning.update_artist_affinity`: Dislike lowers the artist weight (floor
     * ARTIST_MIN_WEIGHT); love/like recover it toward 0. Never positive, never a ban.
     */
    fun updateArtistAffinity(artist: String, rating: String) {
        val a = artistKey(artist)
        if (a.isEmpty() || rating !in setOf("dislike", "love", "like")) return
        val row = store.getWeight("artist", a)
        var w = row?.weight ?: 0.0
        if (rating == "dislike") {
            w = maxOf(EngineConfig.ARTIST_MIN_WEIGHT, w - EngineConfig.ARTIST_DISLIKE_STEP)
        } else {
            if (row == null) return
            w = minOf(0.0, w + EngineConfig.ARTIST_RECOVER_STEP)
        }
        store.putWeight("artist", a, WeightRow(1.0, 1.0, w), clock())
    }

    /** `learning.artist_weights`: "artist:<key>" → weight (< 0) for down-weighted artists. */
    fun artistWeights(): Map<String, Double> =
        store.weightsInScope("artist").filterValues { it.weight < 0 }.entries.associate { "artist:${it.key}" to it.value.weight }

    fun updateFromImplicit(features: Map<String, Double>, event: String, lane: String = "", cluster: String = "") {
        if (event == "not_now") return
        val soft = IMPLICIT_VALUE[event] ?: return
        val rating = when {
            soft >= 0.85 -> "love"
            soft >= 0.6 -> "like"
            soft >= 0.35 -> "meh"
            else -> "skip"
        }
        updateFromRating(features.mapValues { it.value * 0.5 }, rating, lane, cluster)
    }

    fun lessLike(factor: String, lane: String = "", cluster: String = "", amount: Double = 0.35) {
        ensurePriors()
        val feat = when {
            factor in DEFAULT_FEATURES -> factor
            "m_$factor" in DEFAULT_FEATURES -> "m_$factor"
            else -> factor
        }
        val now = clock()
        val scopes = buildList {
            add("global")
            if (lane.isNotEmpty()) add("lane:$lane")
            if (cluster.isNotEmpty()) add("cluster:$cluster")
        }
        store.tx {
            for (scope in scopes) {
                val r = store.getWeight(scope, feat) ?: WeightRow(1.5, 1.5, 0.0)
                store.putWeight(scope, feat, WeightRow(r.alpha, r.beta + amount * 2.0, r.weight - amount), now)
            }
        }
    }

    private fun get(scope: String, feat: String): WeightRow = store.getWeight(scope, feat) ?: WeightRow(1.0, 1.0, 0.0)

    /**
     * `thompson_sample_weights`: with probability EXPLORATION_RATE sample Betas,
     * else posterior means, shrunk toward global. [explore] overrides the coin.
     */
    fun sampleWeights(lane: String, cluster: String, random: Random, explore: Boolean? = null): Map<String, Double> {
        ensurePriors()
        val doExplore = explore ?: (random.nextDouble() < EngineConfig.EXPLORATION_RATE)
        val out = LinkedHashMap<String, Double>()
        for (feat in DEFAULT_FEATURES) {
            val g = get("global", feat)
            val l = if (lane.isNotEmpty()) get("lane:$lane", feat) else g
            val c = if (cluster.isNotEmpty()) get("cluster:$cluster", feat) else g
            fun blend(local: WeightRow, global: WeightRow): Double {
                val n = (local.alpha + local.beta) - 2.0
                val shrink = n / (n + 8.0)
                return if (doExplore) {
                    shrink * sampleBeta(local.alpha, local.beta, random) + (1 - shrink) * sampleBeta(global.alpha, global.beta, random)
                } else {
                    shrink * (local.alpha / (local.alpha + local.beta)) + (1 - shrink) * (global.alpha / (global.alpha + global.beta))
                }
            }
            val base = blend(c, g)
            val laneV = blend(l, g)
            val offset = 0.25 * (c.weight + l.weight + g.weight) / 3.0
            out[feat] = 0.5 * base + 0.5 * laneV + offset
        }
        return out
    }

    private fun sampleBeta(a: Double, b: Double, r: Random): Double {
        val x = sampleGamma(maxOf(a, 1e-3), r)
        val y = sampleGamma(maxOf(b, 1e-3), r)
        return if (x + y == 0.0) 0.5 else x / (x + y)
    }

    /** Marsaglia–Tsang gamma sampler (shape k, scale 1). */
    private fun sampleGamma(k: Double, r: Random): Double {
        if (k < 1.0) return sampleGamma(k + 1.0, r) * r.nextDouble().pow(1.0 / k)
        val d = k - 1.0 / 3.0
        val c = 1.0 / sqrt(9.0 * d)
        while (true) {
            var x: Double
            var v: Double
            do {
                x = gaussian(r)
                v = 1.0 + c * x
            } while (v <= 0)
            v = v * v * v
            val u = r.nextDouble()
            if (u < 1 - 0.0331 * x * x * x * x) return d * v
            if (ln(u) < 0.5 * x * x + d * (1 - v + ln(v))) return d * v
        }
    }

    private fun gaussian(r: Random): Double {
        var u: Double
        do { u = r.nextDouble() } while (u <= 1e-300)
        val v = r.nextDouble()
        return sqrt(-2.0 * ln(u)) * kotlin.math.cos(2 * Math.PI * v)
    }
}

/** Port of `tags/vocabulary.py`: open-ended tag/artist vocabulary + genre chips. */
class Vocabulary(private val store: EngineStore, private val clock: () -> Double = { System.currentTimeMillis() / 1000.0 }) {
    fun observe(tagIn: String, kind: String = "tag", n: Int = 1) {
        val tag = Py.collapseWs(tagIn.trim())
        if (tag.isEmpty() || Py.casefold(tag) in setOf("unknown", "various", "v.a.", "va")) return
        val requested = Py.casefold(kind.trim()).ifEmpty { "tag" }
        if (!Sanitize.isVocabEligible(tag, requested)) return
        val k = Sanitize.inferTagKind(tag, requested)
        if (k == "junk") return
        val now = clock()
        val row = store.getTag(tag, k)
        if (row != null) {
            val newCount = row.count + n
            val dial = newCount >= EngineConfig.TAG_DIAL_THRESHOLD && Sanitize.isDialWorthy(tag, k)
            store.putTag(row.copy(count = newCount, lastSeen = now, asDial = row.asDial || dial))
        } else {
            val dial = n >= EngineConfig.TAG_DIAL_THRESHOLD && Sanitize.isDialWorthy(tag, k)
            store.putTag(TagRow(tag, k, n, dial, now, now))
        }
    }

    /** The (tag, kind) row [observe] would touch, or null when it would ignore the tag. */
    fun rowKey(tagIn: String, kind: String = "tag"): Pair<String, String>? {
        val tag = Py.collapseWs(tagIn.trim())
        if (tag.isEmpty() || Py.casefold(tag) in setOf("unknown", "various", "v.a.", "va")) return null
        val requested = Py.casefold(kind.trim()).ifEmpty { "tag" }
        if (!Sanitize.isVocabEligible(tag, requested)) return null
        val k = Sanitize.inferTagKind(tag, requested)
        return if (k == "junk") null else tag to k
    }

    fun dialTags(limit: Int = 100): List<String> =
        store.listTags(dialsOnly = true, limit = limit).filter { Sanitize.isDialWorthy(it.tag, it.kind.ifEmpty { "tag" }) }.map { it.tag }

    /** Known clusters (except "other") first, then dial-worthy genre/mood tags A→Z. */
    fun availableGenreChips(limit: Int = 40): List<String> {
        val known = EngineConfig.DEFAULT_CLUSTERS.filter { it.isNotEmpty() && it != "other" }
        val seen = known.map { Py.casefold(it) }.toMutableSet()
        val extra = ArrayList<String>()
        for (row in store.listTags(dialsOnly = true, limit = limit * 3)) {
            val kind = Py.casefold(row.kind)
            if (kind != "genre" && kind != "mood") continue
            if (!Sanitize.isDialWorthy(row.tag, kind)) continue
            val key = Py.casefold(row.tag)
            if (key in seen) continue
            seen += key
            extra += row.tag
            if (extra.size >= limit) break
        }
        extra.sortWith { a, b -> Py.casefold(a).compareTo(Py.casefold(b)) }
        return known + extra
    }
}
