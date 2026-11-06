package com.brian.musicrec.core.engine

import java.time.LocalDate
import java.time.temporal.ChronoUnit
import kotlin.math.abs
import kotlin.math.exp
import kotlin.math.ln

/** Port of `engine/reissues.py`. */
object Reissues {
    private val REISSUE_TITLE_RE = Py.regex(
        "(" +
            "\\bremaster(?:ed)?\\b|\\bre[\\s-]?issue\\b|\\bre[\\s-]?release\\b|\\banniversary\\b|" +
            "\\bdeluxe\\b|\\bsuper\\s*deluxe\\b|\\bexpanded\\b|\\bbonus\\s*track\\b|" +
            "\\bresurrect(?:ion|ed)\\b|\\bremix(?:ed)?\\b|\\brework\\b|\\bbootleg\\b|" +
            "\\blive\\s+at\\b|\\bfrom\\s+the\\s+.+\\s+sessions\\b|" +
            "\\bfrom\\s+[\"'][^\"']+[\"']|" +
            "\\b\\d{4}\\s+remaster\\b|" +
            "\\(\\s*(?:19|20)\\d{2}\\s*\\)|" +
            "\\boriginal\\s+soundtrack\\b|\\bsoundtrack\\b|\\bost\\b|" +
            "\\b(?:main|end|love|flying|opening|closing)\\s+theme\\b" +
            ")",
    )

    fun titleLooksLikeReissue(title: String, album: String = ""): Boolean =
        Py.search(REISSUE_TITLE_RE, "$title $album")

    fun isReissueOrOld(
        title: String, album: String = "", releaseDate: String = "",
        originalDate: String? = null, freshDays: Int = 30, today: LocalDate,
    ): Boolean {
        if (titleLooksLikeReissue(title, album)) return true
        val orig = if (!originalDate.isNullOrEmpty()) parseDate(originalDate) else null
        val rel = if (releaseDate.isNotEmpty()) parseDate(releaseDate) else null
        if (orig != null && ChronoUnit.DAYS.between(orig, today) > freshDays) return true
        if (rel != null && ChronoUnit.DAYS.between(rel, today) > freshDays) return true
        return false
    }

    fun parseDate(s: String?): LocalDate? {
        val t = (s ?: "").trim().take(10)
        if (t.isEmpty()) return null
        parseYmd(t)?.let { return it }
        Regex("^(\\d{4})-(\\d{1,2})$").matchEntire(t)?.let {
            return runCatching { LocalDate.of(it.groupValues[1].toInt(), it.groupValues[2].toInt(), 1) }.getOrNull()
        }
        Regex("^(\\d{4})$").matchEntire(t)?.let {
            return runCatching { LocalDate.of(it.groupValues[1].toInt(), 1, 1) }.getOrNull()
        }
        return null
    }

    /** `datetime.strptime(s[:10], "%Y-%m-%d")` (accepts non-padded month/day). */
    fun parseYmd(s: String?): LocalDate? {
        val t = (s ?: "").take(10)
        val m = Regex("^(\\d{4})-(\\d{1,2})-(\\d{1,2})$").matchEntire(t) ?: return null
        return try {
            LocalDate.of(m.groupValues[1].toInt(), m.groupValues[2].toInt(), m.groupValues[3].toInt())
        } catch (_: Exception) {
            null
        }
    }
}

/** Port of `engine/originality.py`. */
object Originality {
    fun isBlocked(c: Candidate, blockedKeys: Set<String>, blockedVideoIds: Set<String>): Boolean {
        if (c.key() in blockedKeys) return true
        val vid = c.externalIds["youtube_id"] ?: ""
        if (vid.isNotEmpty() && vid in blockedVideoIds) return true
        if (c.title.trim().isEmpty()) return true
        if (c.artist.trim().isEmpty() || c.artist == "Unknown") {
            if (c.sourceSignals.isEmpty()) return true
        }
        return false
    }

    fun mainstreamPenalty(c: Candidate): Double {
        val pop = c.popularity ?: return 0.15
        if (pop > 10_000_000) return minOf(1.0, pop / 500_000_000.0)
        if (pop >= EngineConfig.MAINSTREAM_FAN_THRESHOLD) {
            return minOf(1.0, pop / (EngineConfig.MAINSTREAM_FAN_THRESHOLD * 4))
        }
        return maxOf(0.0, pop / EngineConfig.MAINSTREAM_FAN_THRESHOLD) * 0.5
    }

    fun enforceArtistDiversity(cands: List<Candidate>, maxPer: Int = EngineConfig.MAX_PER_ARTIST_BATCH): List<Candidate> {
        val counts = HashMap<String, Int>()
        val out = ArrayList<Candidate>()
        for (c in cands) {
            val a = Py.casefold(c.artist)
            if ((counts[a] ?: 0) >= maxPer) continue
            counts[a] = (counts[a] ?: 0) + 1
            out += c
        }
        return out
    }
}

/** Port of `engine/session.py`: per-process suppress after meh/skip. */
class SessionMemory {
    val blockedKeys = HashSet<String>()
    val blockedPatterns = HashMap<String, MutableSet<String>>()

    fun recordServed(artist: String, title: String) {
        blockedKeys += normalizeKey(artist, title)
    }

    fun recordNegative(artist: String, title: String, rating: String) {
        if (rating != "meh" && rating != "skip" && rating != "dislike") return
        recordServed(artist, title)
        val reasons = TitleFilters.classify(title, artist).reasons
        if (reasons.isEmpty()) return
        val a = Py.casefold(artist).trim()
        if (a.isEmpty()) return
        blockedPatterns.getOrPut(a) { HashSet() }.addAll(reasons)
    }

    fun isSuppressed(c: Candidate): Boolean {
        if (c.key() in blockedKeys) return true
        val blocked = blockedPatterns[Py.casefold(c.artist).trim()] ?: return false
        if (blocked.isEmpty()) return false
        return TitleFilters.classify(c.title, c.artist).reasons.any { it in blocked }
    }
}

/** Port of `engine/candidate_quality.py`. */
object CandidateQuality {
    fun stampClusterTags(c: Candidate): Candidate {
        if (c.cluster.isEmpty() || c.cluster == "other") c.cluster = Clusters.assign(c.tags, c.artist, c.title)
        if (c.cluster.isNotEmpty() && c.cluster != "other") {
            val existing = c.tags.map { Py.casefold(it) }.toSet()
            if (Py.casefold(c.cluster) !in existing) c.tags = c.tags + c.cluster
        }
        c.tags = c.tags.filter { Sanitize.isVocabEligible(it) }
        c.relatedToArtists = Sanitize.sanitizeRelatedArtists(c.relatedToArtists)
        return c
    }

    fun tagSet(c: Candidate): Set<String> {
        val s = c.tags.filter { it.isNotEmpty() }.map { Py.casefold(it).trim() }.toMutableSet()
        if (c.cluster.isNotEmpty()) s += Py.casefold(c.cluster).trim()
        return s
    }

    /** OR-include: true when any tag/cluster equals an enabled genre (casefold, exact). */
    fun matchesEnabledGenres(c: Candidate, enabled: Collection<String>?): Boolean {
        val cf = enabled.orEmpty().filter { it.trim().isNotEmpty() }.map { Py.casefold(it).trim() }.toSet()
        if (cf.isEmpty()) return false
        return tagSet(c).any { it in cf }
    }

    data class Result(val kept: List<Candidate>, val stats: LinkedHashMap<String, Int>)

    /** `enabledGenres` null = unconstrained; empty = nothing matches; else OR-include. */
    fun apply(cands: List<Candidate>, session: SessionMemory?, enabledGenres: Set<String>?): Result {
        val stats = linkedMapOf("hard_drop" to 0, "session_suppress" to 0, "genre_miss" to 0, "kept" to 0, "in" to cands.size)
        val out = ArrayList<Candidate>()
        for (c in cands) {
            if (TitleFilters.classify(c.title, c.artist).action == "drop") {
                stats["hard_drop"] = stats["hard_drop"]!! + 1; continue
            }
            stampClusterTags(c)
            if (session != null && session.isSuppressed(c)) {
                stats["session_suppress"] = stats["session_suppress"]!! + 1; continue
            }
            if (enabledGenres != null && !matchesEnabledGenres(c, enabledGenres)) {
                stats["genre_miss"] = stats["genre_miss"]!! + 1; continue
            }
            out += c
            stats["kept"] = stats["kept"]!! + 1
        }
        return Result(out, stats)
    }
}

/** Port of `eval/calibration.py` (apply + Platt fit). */
object Calibration {
    data class Calib(val kind: String, val a: Double = 0.0, val b: Double = 0.0, val n: Int = 0)

    fun apply(rawConfidence: Double, calib: Calib?): Double {
        val p = maxOf(1e-3, minOf(1 - 1e-3, rawConfidence))
        if (calib == null || calib.kind == "prior") {
            val logit = ln(p / (1 - p))
            return Py.round(1.0 / (1.0 + exp(-1.4 * logit)), 3)
        }
        if (calib.kind == "platt") {
            val z = calib.a * ln(p / (1 - p)) + calib.b
            return Py.round(1.0 / (1.0 + exp(-z)), 3)
        }
        return Py.round(p, 3)
    }

    fun rawConfidenceFromSignals(signals: Map<String, Double>, lane: String): Double {
        if (signals.isEmpty()) return 0.2
        val vals = signals.values.filter { it > 0 }
        val strength = vals.sum() / maxOf(1.0, vals.size.toDouble())
        val n = vals.size
        // Python: max() of an empty list raises; engine always has ≥1 positive here.
        val entropyProxy = if (vals.isEmpty()) 0.0 else 1.0 - (vals.max() / (vals.sum() + 1e-6))
        var base = 0.25 + 0.35 * strength + 0.25 * minOf(1.0, n / 4.0) + 0.15 * entropyProxy
        val h = signals.keys.sorted().joinToString("").codePoints().sum() % 100
        base += (h - 50) / 1000.0
        if (lane == "fresh") base = 0.15 + 0.7 * base
        return maxOf(0.05, minOf(0.95, base))
    }

    private val RATING_POS = mapOf("love" to 1.0, "like" to 1.0, "meh" to 0.0, "skip" to 0.0, "dislike" to 0.0)

    /** `fit_calibration`: Platt scaling once ≥20 rated predictions exist, else the spread prior. */
    fun fit(pairs: List<Pair<Double, String>>): Calib {
        val pts = pairs.mapNotNull { (p, r) -> RATING_POS[r]?.let { p to it } }
        if (pts.size < 20) return Calib("prior", 0.0, 0.0, pts.size)
        val xs = pts.map { val c = minOf(1 - 1e-3, maxOf(1e-3, it.first)); ln(c / (1 - c)) }
        val ys = pts.map { it.second }
        var a = 1.0
        var b = 0.0
        repeat(50) {
            var gA = 0.0; var gB = 0.0; var hAA = 0.0; var hBB = 0.0; var hAB = 0.0
            for (i in xs.indices) {
                val p = 1.0 / (1.0 + exp(-(a * xs[i] + b)))
                val w = p * (1 - p)
                gA += (p - ys[i]) * xs[i]; gB += p - ys[i]
                hAA += w * xs[i] * xs[i]; hBB += w; hAB += w * xs[i]
            }
            gA += 1e-4 * a; gB += 1e-4 * b; hAA += 1e-4; hBB += 1e-4
            val det = hAA * hBB - hAB * hAB
            if (abs(det) < 1e-12) return Calib("platt", a, b, pts.size)
            val da = (hBB * gA - hAB * gB) / det
            val db = (hAA * gB - hAB * gA) / det
            a -= da; b -= db
            if (abs(da) + abs(db) < 1e-6) return Calib("platt", a, b, pts.size)
        }
        return Calib("platt", a, b, pts.size)
    }
}

/** Port of `engine/scorer.py::score_candidate`. Deterministic given `today`. */
object Scorer {
    private val AUDIO_METRIC_NAMES = setOf(
        "tempo_bpm", "energy", "loudness", "brightness", "percussiveness",
        "danceability", "acousticness", "instrumentalness", "dynamic_range",
        "obscurity", "freshness_days",
    )

    fun score(
        cand: Candidate,
        weights: Map<String, Double>,
        lane: String,
        obscurity: Double = 0.0,
        adventurous: Boolean = false,
        genre: String = "",
        dials: DialSet? = null,
        calib: Calibration.Calib? = null,
        metricsIn: Map<String, MetricValue>? = null,
        today: LocalDate = LocalDate.now(),
    ): Candidate {
        val signals = LinkedHashMap(cand.sourceSignals)
        val metrics: Map<String, MetricValue> = metricsIn ?: cand.metrics

        if (adventurous) signals["adventurous"] = (signals["adventurous"] ?: 0.5) + 0.3
        if (obscurity > 0) {
            val pen = Originality.mainstreamPenalty(cand)
            signals["obscurity"] = maxOf(0.0, obscurity * (1.0 - pen))
        }
        if (genre.isNotEmpty()) {
            val blob = Py.casefold((cand.tags + listOf(cand.artist, cand.title, cand.album)).joinToString(" "))
            signals["tag_overlap"] = maxOf(signals["tag_overlap"] ?: 0.0, if (blob.contains(Py.casefold(genre))) 1.0 else 0.0)
        }
        for ((name, m) in metrics) {
            val feat = "m_$name"
            if (feat !in weights && name !in AUDIO_METRIC_NAMES) continue
            val norm = m.norm ?: continue
            signals[feat] = norm
        }

        var score = 0.0
        val contribs = ArrayList<WhyPart>()
        for ((src, v) in signals) {
            val w = weights[src] ?: 0.5
            val part = v * (0.5 + w)
            score += part
            if (abs(part) > 0.05) contribs += WhyPart(src, Py.round(v, 3), Py.round(w, 3), Py.round(part, 3))
        }
        val nSources = signals.keys.count { !it.endsWith("_chart") && !it.startsWith("m_") && (signals[it] ?: 0.0) > 0 }
        val multi = if (nSources != 0) minOf(1.0, (nSources - 1) * 0.25) else 0.0
        score += multi
        if (multi != 0.0) contribs += WhyPart("source_agreement", nSources.toDouble(), 1.0, Py.round(multi, 3))

        score -= Originality.mainstreamPenalty(cand) * (0.6 + 0.4 * obscurity)

        if (lane == "fresh" && cand.releaseDate.isNotEmpty()) {
            val rd = Reissues.parseYmd(cand.releaseDate)
            if (rd != null) {
                val age = ChronoUnit.DAYS.between(rd, today)
                if (age in 0..EngineConfig.FRESH_DAYS.toLong()) {
                    val bonus = 0.4 * (1.0 - age.toDouble() / EngineConfig.FRESH_DAYS)
                    score += bonus
                    contribs += WhyPart("freshness_age", age.toDouble(), 1.0, Py.round(bonus, 3))
                } else {
                    score -= 0.5
                }
            }
        }

        if (dials != null && metrics.isNotEmpty()) {
            val t = dials.targetScore(metrics)
            score += t * 1.2
            if (t != 0.0) contribs += WhyPart("dial_targets", Py.round(t, 3), 1.2, Py.round(t * 1.2, 3))
        }

        // Per-artist down-weight from Dislike ratings (soft penalty, never a ban)
        val aw = weights["artist:" + Learning.artistKey(cand.artist)]
        if (aw != null && aw < 0) {
            val pen = -aw * EngineConfig.ARTIST_PENALTY_SCALE
            score -= pen
            contribs += WhyPart("artist_dislike", Py.round(aw, 3), EngineConfig.ARTIST_PENALTY_SCALE, Py.round(-pen, 3))
        }

        val sorted = contribs.sortedBy { -abs(it.contrib) } // stable, like Python sort
        val raw = Calibration.rawConfidenceFromSignals(signals.filterKeys { !it.startsWith("m_") }, lane)
        val scoreNorm = 1.0 / (1.0 + exp(maxOf(-20.0, minOf(20.0, -0.35 * (score - 1.5)))))
        val blended = 0.55 * raw + 0.45 * scoreNorm
        val confidence = Calibration.apply(blended, calib)

        cand.score = score
        cand.confidence = confidence
        cand.lane = lane
        cand.whyParts = sorted.take(8)
        cand.why = why(cand, signals, lane, sorted)
        val merged = LinkedHashMap(metrics)
        merged.putAll(Metrics.build(mapOf("like_probability" to confidence)))
        cand.metrics = merged
        return cand
    }

    private fun why(c: Candidate, signals: Map<String, Double>, lane: String, contribs: List<WhyPart>): String {
        val bits = ArrayList<String>()
        if (c.relatedToArtists.isNotEmpty()) bits += "similar to liked artist(s): " + c.relatedToArtists.take(3).joinToString(", ")
        val top = contribs.take(4).map { it.feature }.filter { it != "dial_targets" }
        if (top.isNotEmpty()) bits += "top factors: " + top.joinToString(", ")
        val skip = setOf("artist_overlap", "tag_overlap", "freshness", "obscurity", "adventurous")
        val srcs = signals.filter { (k, v) -> v > 0 && !k.startsWith("m_") && k !in skip }.keys
        if (srcs.isNotEmpty()) bits += "supported by " + srcs.toSortedSet().take(4).joinToString(", ")
        if (lane == "fresh") bits += "fresh release (${c.releaseDate.ifEmpty { "recent" }}); gamble"
        if ((signals["obscurity"] ?: 0.0) > 0.3) bits += "leaning less-mainstream"
        if ((signals["watched_artist"] ?: 0.0) > 0) bits += "favorite-artist new release"
        if (bits.isEmpty()) bits += "exploratory candidate"
        return bits.joinToString("; ")
    }
}
