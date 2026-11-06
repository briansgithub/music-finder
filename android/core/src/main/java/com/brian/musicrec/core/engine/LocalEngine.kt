package com.brian.musicrec.core.engine

import com.brian.musicrec.core.DialState
import com.brian.musicrec.core.Recommendation
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import java.time.LocalDate
import java.time.temporal.ChronoUnit
import kotlin.random.Random

/** Port of `engine/lanes.py`. */
object Lanes {
    fun applyRecency(cands: List<Candidate>, recency: String, today: LocalDate): List<Candidate> {
        val cutoff = when (recency) {
            "week" -> today.minusDays(7)
            "month" -> today.minusDays(30)
            "year" -> today.minusDays(365)
            else -> return cands
        }
        return cands.filter { c -> Reissues.parseYmd(c.releaseDate)?.let { !it.isBefore(cutoff) } ?: false }
    }

    fun contextMetrics(c: Candidate, today: LocalDate): LinkedHashMap<String, MetricValue> {
        var freshnessDays: Double? = null
        var eraYear: Double? = null
        Reissues.parseYmd(c.releaseDate)?.let {
            freshnessDays = ChronoUnit.DAYS.between(it, today).toDouble()
            eraYear = it.year.toDouble()
        }
        val obscurity = c.popularity?.let { maxOf(0.0, minOf(1.0, 1.0 - (it / (EngineConfig.MAINSTREAM_FAN_THRESHOLD * 2)))) }
        val nSrc = c.sourceSignals.count { (k, v) -> v > 0 && !k.startsWith("m_") }
        return Metrics.build(
            linkedMapOf(
                "freshness_days" to freshnessDays, "obscurity" to obscurity,
                "era_year" to eraYear, "source_agreement" to nSrc.toDouble(),
            ),
        )
    }

    private fun mergedMetrics(store: EngineStore, c: Candidate, today: LocalDate): LinkedHashMap<String, MetricValue> {
        val m = LinkedHashMap(store.metricsFor(c.key()) ?: emptyMap())
        m.putAll(contextMetrics(c, today))
        return m
    }

    fun selectProven(
        store: EngineStore, cands: List<Candidate>, weights: Map<String, Double>,
        obscurity: Double = 0.0, adventurous: Boolean = false, genre: String = "",
        dials: DialSet? = null, calib: Calibration.Calib? = null, today: LocalDate,
    ): Pair<List<Candidate>, Map<String, Int>> {
        val scored = ArrayList<Candidate>()
        var rejects = 0
        var weak = 0
        for (c in cands) {
            val metrics = mergedMetrics(store, c, today)
            if (dials != null && dials.filters.isNotEmpty() && !dials.passesFilters(metrics).first) { rejects++; continue }
            Scorer.score(c, weights, "proven", obscurity, adventurous, genre, dials, calib, metrics, today)
            val nSrc = c.sourceSignals.count { (k, v) -> v > 0 && k != "obscurity" && k != "adventurous" }
            val strong = nSrc >= EngineConfig.PROVEN_MIN_SOURCES || (
                nSrc >= 1 && (c.sourceSignals["artist_overlap"] ?: 0.0) >= 0.6 &&
                    c.confidence >= EngineConfig.PROVEN_MIN_CONFIDENCE
                )
            if (strong && c.confidence >= EngineConfig.PROVEN_MIN_CONFIDENCE * 0.85) scored += c else weak++
        }
        val sorted = scored.sortedWith(compareByDescending<Candidate> { it.score }.thenByDescending { it.confidence })
        return sorted to mapOf("filter_rejects" to rejects, "weak_or_low_confidence_rejects" to weak, "scored" to sorted.size)
    }

    fun selectFresh(
        store: EngineStore, cands: List<Candidate>, weights: Map<String, Double>,
        obscurity: Double = 0.0, adventurous: Boolean = false, genre: String = "",
        dials: DialSet? = null, calib: Calibration.Calib? = null, today: LocalDate,
        minConfidence: Double = EngineConfig.FRESH_MIN_CONFIDENCE,
        freshDays: Int = EngineConfig.FRESH_DAYS,
    ): Pair<List<Candidate>, Map<String, Int>> {
        val scored = ArrayList<Candidate>()
        var rejects = 0
        var reissue = 0
        var lowConf = 0
        var tooOld = 0
        var weak = 0
        val cutoff = today.minusDays(freshDays.toLong())
        for (c in cands) {
            // (PC engine optionally enriches original dates from MusicBrainz here; offline we rely on titles/dates
            // and the recording first-release-date stored by the on-device refill as original_date.)
            if (Reissues.isReissueOrOld(c.title, c.album, c.releaseDate, c.externalIds["original_date"], freshDays, today)) {
                reissue++; continue
            }
            val rd = Reissues.parseYmd(c.releaseDate)
            if (rd != null && rd.isBefore(cutoff)) { tooOld++; continue }
            val metrics = mergedMetrics(store, c, today)
            if (dials != null && dials.filters.isNotEmpty() && !dials.passesFilters(metrics).first) { rejects++; continue }
            Scorer.score(c, weights, "fresh", obscurity, adventurous, genre, dials, calib, metrics, today)
            if ((c.sourceSignals["artist_overlap"] ?: 0.0) < 0.15 && (c.sourceSignals["freshness"] ?: 0.0) < 0.5) { weak++; continue }
            if (c.confidence < minConfidence) { lowConf++; continue }
            scored += c
        }
        val sorted = scored.sortedWith(compareByDescending<Candidate> { it.score }.thenByDescending { it.confidence })
        return sorted to mapOf(
            "filter_rejects" to rejects, "reissue_rejects" to reissue, "too_old_rejects" to tooOld,
            "weak_overlap_rejects" to weak, "low_confidence_rejects" to lowConf, "scored" to sorted.size,
        )
    }
}

/**
 * The on-device engine: port of `engine/recommend.py::Recommender` plus the
 * FastAPI surface (`/next`, `/rate`, `/not_now`, `/dials`, `/export`,
 * `/correct_video`). Ranking is the same transparent scoring + Beta/Thompson
 * learning as the PC engine — no AI/LLM anywhere.
 */
class LocalEngine(
    val store: EngineStore,
    net: NetFetcher? = null,
    allowNetworkResolve: () -> Boolean = { false },
    private val random: Random = Random.Default,
    private val clock: () -> Double = { System.currentTimeMillis() / 1000.0 },
    private val today: () -> LocalDate = { LocalDate.now() },
    private val log: (String) -> Unit = {},
) {
    val session = SessionMemory()
    val learning = Learning(store, clock)
    val vocab = Vocabulary(store, clock)
    val resolver = VideoResolver(store, net, allowNetworkResolve, clock, log)
    private var sessionDials = DialSet()
    private var cacheProven: List<Candidate> = emptyList()
    private var cacheFresh: List<Candidate> = emptyList()
    private var builtFor: Any? = null

    /** Fresh lane minimum confidence (Settings-tunable); below it the card comes from proven. */
    var freshMinConfidence: Double = EngineConfig.FRESH_MIN_CONFIDENCE
        @Synchronized set(v) {
            val c = v.coerceIn(0.0, 1.0)
            if (c != field) { field = c; builtFor = null }
        }

    /** How long next() waits for an online video lookup before handing off as a YTM search. */
    var resolveWaitMs: Long = 800

    private val netAvailable = net != null
    private val bg: java.util.concurrent.ExecutorService? = if (net != null) {
        java.util.concurrent.Executors.newSingleThreadExecutor { r -> Thread(r, "mf-video-resolve").apply { isDaemon = true } }
    } else null
    private val pendingResolves = java.util.concurrent.ConcurrentHashMap<String, java.util.concurrent.Future<ResolveResult>>()
    var lastFilterStats: Map<String, Any> = emptyMap()
        private set

    // ---- batch 2: affinity / diversity / supply / friends ----
    /** Random per engine process; logged with every served card. */
    val sessionId: String = java.util.UUID.randomUUID().toString()
    val modelVersion: String = "${EngineConfig.VERSION}/${Affinity.MODEL_VERSION}"
    private val history = ArrayList<Diversity.Served>()
    private var model: Affinity.Model? = null
    /** Set when lane=fresh had nothing to offer (UI shows it; optional 90-day expansion). */
    var lastExhaustion: Map<String, Any>? = null
        private set
    /** null = 30-day fresh window; 90 = labeled expansion (Settings / exhaustion prompt). */
    var freshExpandDays: Int? = null
        @Synchronized set(v) { if (v != field) { field = v; builtFor = null } }
    /** Last.fm API key (Settings). Blank = Last.fm sources skipped. */
    @Volatile var lastfmApiKey: String = ""
    val queue = RequestQueue(store, net, clock, log)
    private val refiller = Refill(store, queue, clock, today, log)

    init { resolver.queue = queue }
    var lastSlot: String = ""
        private set

    /** Everything needed to reverse one rating / not-now exactly. */
    private data class UndoEntry(
        val kind: String,
        val rec: Recommendation,
        val ratingId: Long?,
        val feedbackId: Long?,
        val weights: List<Pair<Pair<String, String>, WeightRow?>>,
        val tags: List<Pair<Pair<String, String>, TagRow?>>,
        val artistCf: String,
        val sessionPatterns: Set<String>?,
    )

    private val undoStack = ArrayDeque<UndoEntry>()
    /** key -> recommendations_log id of its latest serve (to un-serve a displaced card on undo). */
    private val servedLogIds = HashMap<String, Long>()

    data class UndoResult(val rec: Recommendation, val remaining: Int)

    companion object {
        const val MAX_UNDO = 10
        /** audience:group utilities: group reactions + nothing else. */
        val AUDIENCE_UTILITY: Map<String, Double?> = BroadAppeal.REACTION_UTILITY.mapKeys { "group_${it.key}" }
    }

    @Synchronized
    fun hasSeed(): Boolean = store.meta("seed_version") != null

    /** Raw `exported_at` of the last imported seed bundle (to detect a newer bundled seed). */
    @Synchronized
    fun seedExportedRaw(): String? = store.meta("seed_exported_raw")

    @Synchronized
    fun importText(text: String): SeedImporter.Summary {
        val s = SeedImporter(store).importText(text, clock())
        // Replay taste updates for ratings that came from the PC (default features; PC weights not in /export).
        for (r in s.newRatings) {
            if (s.kind == "seed") continue // seed bundle already carries the PC's learned weights
            learning.updateFromRating(featuresFor(r.key, r.lane), r.rating, r.lane.ifEmpty { "proven" }, r.cluster)
            learning.updateArtistAffinity(r.artist, r.rating)
        }
        if (s.kind == "export" && s.enabledGenresPresent) {
            sessionDials = sessionDials.copy(enabledGenres = s.enabledGenres?.toSet())
        }
        builtFor = null
        return s
    }

    private fun ensurePool(
        genre: String = "", adventurous: Boolean = false, obscurity: Double = 0.0,
        recency: String = "any", dials: DialSet?, force: Boolean = false,
    ) {
        val dialKey = listOf(
            dials?.filters?.toSortedMap()?.toList(), dials?.targets?.toSortedMap()?.toList(),
            dials?.preset ?: "", dials?.enabledGenresKey(),
        )
        val key = listOf(genre, adventurous, Py.round(obscurity, 2), recency, dialKey)
        if (!force && builtFor == key && (cacheProven.isNotEmpty() || cacheFresh.isNotEmpty())) return
        learning.ensurePriors()
        val day = today()
        val blocked = store.blockedKeys()
        val blockedVids = store.blockedVideoIds()
        val calib = store.latestCalibration()

        val allProven = store.candidates("proven")
        val allFresh = store.candidates("fresh")
        val nProven = allProven.size
        val nFresh = allFresh.size
        val provenRawAll = allProven.filter { !Originality.isBlocked(it, blocked, blockedVids) }
        val freshRawAll = allFresh.filter { !Originality.isBlocked(it, blocked, blockedVids) }
        var provenRaw = provenRawAll
        var freshRaw = freshRawAll
        val enabled = dials?.enabledGenres
        val qp = CandidateQuality.apply(provenRaw, session, enabled)
        val qf = CandidateQuality.apply(freshRaw, session, enabled)
        provenRaw = qp.kept
        freshRaw = qf.kept
        if (recency.isNotEmpty() && recency != "any") {
            provenRaw = Lanes.applyRecency(provenRaw, recency, day)
            freshRaw = Lanes.applyRecency(freshRaw, recency, day)
        }
        val cluster = Clusters.assign(if (genre.isNotEmpty()) listOf(genre) else emptyList(), "", "")
        val artistW = learning.artistWeights() // Dislike down-weights (soft score penalty)
        val wProven = learning.sampleWeights("proven", cluster, random) + artistW
        val wFresh = learning.sampleWeights("fresh", cluster, random) + artistW
        val freshDays = freshExpandDays ?: EngineConfig.FRESH_DAYS
        val (proven, ps) = Lanes.selectProven(store, provenRaw, wProven, obscurity, adventurous, genre, dials, calib, day)
        val (fresh, fs) = Lanes.selectFresh(store, freshRaw, wFresh, obscurity, adventurous, genre, dials, calib, day, freshMinConfidence, freshDays)
        val m = buildModel(provenRawAll + freshRawAll)
        model = m
        // Artist spacing is now done per card by MMR (Diversity), not by dropping candidates here.
        cacheProven = Affinity.rank(m, proven)
        cacheFresh = Affinity.rank(m, fresh)
        lastFilterStats = mapOf(
            "proven" to ps, "fresh" to fs, "quality" to mapOf("proven" to qp.stats, "fresh" to qf.stats),
            "funnel" to mapOf(
                "proven" to funnel(nProven, nProven - provenRawAll.size, qp.stats, provenRawAll.size - qp.kept.size, qp.kept.size - provenRaw.size, ps, cacheProven.size),
                "fresh" to funnel(nFresh, nFresh - freshRawAll.size, qf.stats, freshRawAll.size - qf.kept.size, qf.kept.size - freshRaw.size, fs, cacheFresh.size),
            ),
            "fresh_days" to freshDays,
            "model" to modelVersion,
        )
        builtFor = key
        log("pool rebuilt: proven=${cacheProven.size} fresh=${cacheFresh.size} (fresh window $freshDays d) funnel=${lastFilterStats["funnel"]}")
    }

    /** `Recommender.next` */
    @Synchronized
    fun nextCandidate(
        lane: String = "auto", genre: String = "", adventurous: Boolean = false,
        obscurity: Double = 0.0, recency: String = "any", dials: DialSet? = null,
    ): Candidate? {
        val lanePref = if (dials != null && dials.lane.isNotEmpty() && dials.lane != "auto") dials.lane else lane
        ensurePool(genre, adventurous, obscurity, recency, dials)
        lastExhaustion = null
        val chosen = Diversity.chooseLane(lanePref, cacheProven, cacheFresh, history)
        if (chosen == null) {
            if (lanePref == "fresh") {
                @Suppress("UNCHECKED_CAST")
                lastExhaustion = mapOf(
                    "lane" to "fresh", "fresh_days" to (freshExpandDays ?: EngineConfig.FRESH_DAYS),
                    "can_expand" to (freshExpandDays == null),
                    "funnel" to ((lastFilterStats["funnel"] as? Map<String, Any>)?.get("fresh") ?: emptyMap<String, Any>()),
                )
                log("fresh lane exhausted: ${lastExhaustion}")
            }
            return null
        }
        val pool = if (chosen == "fresh") cacheFresh else cacheProven
        val m = model ?: return null
        val pick = Diversity.pick(pool, history, m.stats) ?: return null
        val cand = pick.cand
        lastSlot = pick.slot
        cacheProven = cacheProven.filter { it.key() != cand.key() }
        cacheFresh = cacheFresh.filter { it.key() != cand.key() }
        session.recordServed(cand.artist, cand.title)
        history += Diversity.served(cand)
        val parts = Affinity.parts(m, cand)
        val snapshot = JsonCodec.metricsJson(cand.metrics).toMutableMap()
        snapshot["_log"] = buildJsonObject {
            put("session", sessionId); put("model", modelVersion); put("slot", pick.slot); put("slot_no", history.size)
            put("lane_choice", chosen); put("affinity", cand.affinity); put("selection", cand.selection)
            put("p_like", cand.confidence); put("legacy_score", cand.score); put("mmr_penalty", pick.mmrPenalty)
            put("spacing_relaxed", pick.relaxed); put("seed", parts.seed); put("tags", parts.tags)
            put("artist", parts.artist); put("label", parts.label)
        }
        servedLogIds[cand.key()] = store.logRecommendation(
            LogRow(
                cand.artist, cand.title, cand.key(), cand.lane, cand.confidence, cand.why,
                JsonCodec.whyPartsJson(cand.whyParts).toString(), JsonObject(snapshot).toString(), clock(),
            ),
        )
        log("serve[$sessionId #${history.size}] ${pick.slot} ${cand.lane} ${cand.artist} – ${cand.title} Z=${"%.3f".format(cand.affinity)} sel=${"%.3f".format(cand.selection)} p=${"%.3f".format(cand.confidence)} mmr=${"%.3f".format(pick.mmrPenalty)}")
        return cand
    }

    /** `GET /next` — `enabledGenres` null = use session dials; else OR-include allow-list for this call. */
    @Synchronized
    fun next(lane: String = "auto", enabledGenres: List<String>? = null): Recommendation? {
        var dials = sessionDials
        if (lane.isNotEmpty() && lane != "auto") dials = dials.withLane(lane)
        if (enabledGenres != null) dials = dials.copy(enabledGenres = enabledGenres.map { it.trim() }.filter { it.isNotEmpty() }.toSet())
        val c = nextCandidate(lane = lane, dials = dials) ?: return null
        val rec = toRecommendation(c)
        prefetchVideoIds()
        return rec
    }

    private fun candVideoId(c: Candidate): String =
        YouTubeIds.extract(c.youtubeUrl) ?: c.externalIds["youtube"] ?: c.externalIds["youtube_id"] ?: ""

    /** Queue (or join) a background online lookup; never runs on the caller's thread. */
    private fun resolveInBackground(artist: String, title: String, candVid: String): java.util.concurrent.Future<ResolveResult>? {
        val ex = bg ?: return null
        val key = normalizeKey(artist, title)
        pendingResolves[key]?.let { if (!it.isDone || runCatching { it.get().ok }.getOrDefault(false)) return it }
        if (pendingResolves.size > 200) pendingResolves.entries.removeIf { it.value.isDone }
        return ex.submit<ResolveResult> { resolver.resolve(artist, title, candVid, allowNetworkLookup = true) }
            .also { pendingResolves[key] = it }
    }

    /** Look up video IDs for the next few pool heads in the background so their cards open as exact videos. */
    private fun prefetchVideoIds(n: Int = 3) {
        if (!netAvailable) return
        for (c in (cacheProven.take(n - 1) + cacheFresh.take(1))) {
            val local = resolver.resolve(c.artist, c.title, candVideoId(c), allowNetworkLookup = false)
            if (!local.ok && resolver.canLookup(c.artist, c.title)) resolveInBackground(c.artist, c.title, candVideoId(c))
        }
    }

    /**
     * Wait (off the engine lock) up to [timeoutMs] for a pending background lookup
     * of [r]; returns the upgraded card when a video ID was found, else null.
     */
    fun awaitVideo(r: Recommendation, timeoutMs: Long = 10_000): Recommendation? {
        if (r.videoId.length == 11) return null
        val f = pendingResolves[normalizeKey(r.artist, r.title)] ?: return null
        val res = try { f.get(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS) } catch (_: Exception) { return null }
        // Re-read locally so a saved "wrong song" correction still wins over the lookup.
        val final = resolver.resolve(r.artist, r.title, "", allowNetworkLookup = false).takeIf { it.ok } ?: res
        if (!final.ok || final.videoId.length != 11) return null
        return withVideo(r, final.videoId, final.source)
    }

    private fun withVideo(r: Recommendation, vid: String, source: String) = r.copy(
        videoId = vid, ytmUrl = YouTubeIds.watchUrl(vid), ytmHandoff = "watch",
        ytmIntent = YouTubeIds.intentUri(r.artist, r.title, vid), resolveSource = source,
    )

    private fun toRecommendation(c: Candidate, observe: Boolean = true, slot: String = lastSlot): Recommendation {
        val candVid = candVideoId(c)
        // Local first (bundled/pre-resolved IDs, cache, seeds, correction) — instant.
        var res = resolver.resolve(c.artist, c.title, candVid, allowNetworkLookup = false)
        if (!res.ok && resolver.canLookup(c.artist, c.title)) {
            // Online lookup runs in the background; wait only briefly so next() stays fast.
            val f = resolveInBackground(c.artist, c.title, candVid)
            res = try {
                f?.get(resolveWaitMs, java.util.concurrent.TimeUnit.MILLISECONDS) ?: res
            } catch (_: java.util.concurrent.TimeoutException) {
                log("resolve: ${c.artist} – ${c.title} still looking up in background (>${resolveWaitMs} ms)")
                res.copy(status = "pending", chain = res.chain + "background")
            } catch (e: Exception) {
                res
            }
        }
        val vid = if (res.ok && res.videoId.length == 11) res.videoId else ""
        if (observe) {
            vocab.observe(c.artist, "artist")
            if (c.cluster.isNotEmpty() && c.cluster != "other") vocab.observe(c.cluster, "genre")
            c.tags.forEach { vocab.observe(it, "tag") }
        }
        val search = YouTubeIds.searchUrl(c.artist, c.title)
        val nRatings = model?.ratingsCount ?: 0
        val expanded = c.lane == "fresh" && (freshExpandDays ?: EngineConfig.FRESH_DAYS) > EngineConfig.FRESH_DAYS
        return Recommendation(
            artist = c.artist, title = c.title, lane = c.lane, confidence = c.confidence, score = c.score,
            why = c.why, ytmUrl = if (vid.isNotEmpty()) YouTubeIds.watchUrl(vid) else search,
            ytmSearchUrl = search, ytmIntent = YouTubeIds.intentUri(c.artist, c.title, vid),
            ytmHandoff = if (vid.isNotEmpty()) "watch" else "search_fallback",
            videoId = vid, tags = c.tags, cluster = c.cluster, resolveSource = res.source,
            affinity = c.affinity, selection = c.selection, slot = slot,
            matchLabel = Affinity.matchLabel(c.affinity, nRatings, c.confidence, slot == "exploration"),
            laneLabel = if (expanded) "fresh (${freshExpandDays}-day)" else c.lane,
        )
    }

    /** `POST /rate`. [shown] is the card as displayed (re-shown verbatim on undo). */
    @Synchronized
    fun rate(
        artist: String, title: String, rating: String, lane: String = "", cluster: String = "",
        predictedConfidence: Double? = null, shown: Recommendation? = null,
    ) {
        require(rating in Learning.RATINGS) { "rating must be love|like|meh|skip|dislike|not_now" }
        val key = normalizeKey(artist, title)
        val cl = cluster.ifEmpty { Clusters.assign(emptyList(), artist, title) }
        val conf = predictedConfidence ?: store.lastLog(key)?.confidence
        val rec = shown ?: Recommendation(
            artist = artist, title = title, lane = lane, confidence = conf ?: 0.0, cluster = cl,
            ytmSearchUrl = YouTubeIds.searchUrl(artist, title), ytmUrl = YouTubeIds.searchUrl(artist, title),
            ytmHandoff = "search_fallback",
        )
        val artistCf = Py.casefold(artist).trim()
        val sessionBefore = session.blockedPatterns[artistCf]?.toSet()
        if (rating == "not_now") {
            val fid = store.addFeedback(key, "not_now", artist, title, "", lane, clock())
            pushUndo(UndoEntry("not_now", rec, null, fid, emptyList(), emptyList(), artistCf, sessionBefore))
            // not_now never changes the lane: anything sticky goes back to auto.
            if (sessionDials.lane != "auto") sessionDials = sessionDials.copy(lane = "auto")
            return
        }
        val features = featuresFor(key, lane)
        val learnLane = lane.ifEmpty { "proven" }
        learning.ensurePriors()
        val weightSnap = (learning.affectedKeys(features, rating, learnLane, cl) + learning.artistAffinityKeys(artist, rating))
            .distinct().map { it to store.getWeight(it.first, it.second) }
        val artistRowBefore = learning.artistAffinityKeys(artist, rating).firstOrNull()?.let { store.getWeight(it.first, it.second) }
        val tagSnap = listOfNotNull(vocab.rowKey(artist, "artist")).map { it to store.getTag(it.first, it.second) }
        if (rating == "meh" || rating == "skip" || rating == "dislike") {
            session.recordNegative(artist, title, rating)
            builtFor = null
        }
        // Artist penalty changed → rebuild the pool.
        if (rating == "dislike" || (artistRowBefore != null && artistRowBefore.weight < 0)) builtFor = null
        // Affinity stats are derived from ratings → rebuild the pool so the next card reflects this rating.
        builtFor = null
        if (store.meta("feat:$key") == null) {
            store.setMeta("feat:$key", Affinity.features(artist, rec.tags, rec.cluster.ifEmpty { cl }, null).joinToString("\u0001"))
        }
        val rid = store.tx {
            val id = store.addRating(RatingRow(artist, title, key, rating, lane, cl, conf, clock()))
            learning.updateFromRating(features, rating, learnLane, cl)
            learning.updateArtistAffinity(artist, rating)
            vocab.observe(artist, "artist")
            id
        }
        pushUndo(UndoEntry(rating, rec, rid, null, weightSnap, tagSnap, artistCf, sessionBefore))
    }

    /** `POST /not_now`: postpone, no taste penalty. */
    @Synchronized
    fun notNow(artist: String, title: String, lane: String = "", shown: Recommendation? = null) =
        rate(artist, title, "not_now", lane, shown = shown)

    private fun pushUndo(e: UndoEntry) {
        undoStack.addLast(e)
        while (undoStack.size > MAX_UNDO) undoStack.removeFirst()
    }

    /** Keys in the ready pools after (re)building with the session dials (Debug/tests). */
    @Synchronized
    fun readyPoolKeys(): Set<String> {
        ensurePool(dials = sessionDials)
        return (cacheProven + cacheFresh).map { it.key() }.toSet()
    }

    @Synchronized
    fun undoDepth(): Int = undoStack.size

    /**
     * `POST /undo`: revert the most recent rating / not-now exactly — delete its
     * row, restore the pre-update weight + tag rows, restore session suppression —
     * and return that card to re-show. [displacedKey] (the card currently on
     * screen, served after the rating) is un-served so it goes back into the pool.
     */
    @Synchronized
    fun undo(displacedKey: String? = null): UndoResult? {
        val e = undoStack.removeLastOrNull() ?: return null
        val key = normalizeKey(e.rec.artist, e.rec.title)
        store.tx {
            e.ratingId?.let { store.deleteRating(it) }
            e.feedbackId?.let { store.deleteFeedback(it) }
            for ((k, row) in e.weights) {
                if (row == null) store.deleteWeight(k.first, k.second)
                else store.putWeight(k.first, k.second, row, clock())
            }
            for ((k, row) in e.tags) {
                if (row == null) store.deleteTag(k.first, k.second) else store.putTag(row)
            }
            if (displacedKey != null && displacedKey != key) {
                servedLogIds.remove(displacedKey)?.let { store.deleteLog(it) }
                session.blockedKeys.remove(displacedKey)
                history.indexOfLast { it.key == displacedKey }.takeIf { it >= 0 }?.let { history.removeAt(it) }
            }
        }
        if (e.artistCf.isNotEmpty()) {
            if (e.sessionPatterns == null) session.blockedPatterns.remove(e.artistCf)
            else session.blockedPatterns[e.artistCf] = e.sessionPatterns.toMutableSet()
        }
        // The undone card is "current" again (served, not in the pool); rebuild so
        // suppressed variants and the displaced card become available again.
        session.recordServed(e.rec.artist, e.rec.title)
        cacheProven = cacheProven.filter { it.key() != key }
        cacheFresh = cacheFresh.filter { it.key() != key }
        builtFor = null
        return UndoResult(e.rec, undoStack.size)
    }

    // ================= batch 2 helpers =================

    private fun featuresOfJudged(r: RatingRow): List<String> =
        store.meta("feat:${r.key}")?.split('\u0001')?.filter { it.isNotEmpty() }
            ?: Affinity.features(r.artist, emptyList(), r.cluster, null)

    /** Personal profile: one active judgment per recording from the ratings table (exact under undo). */
    private fun personalJudgments(): List<Affinity.Judgment> =
        Affinity.activeJudgments(store.ratings(100_000).asReversed().map { Affinity.Judgment(it.key, it.rating, featuresOfJudged(it)) })

    private fun buildModel(pool: List<Candidate>): Affinity.Model {
        val active = personalJudgments()
        return Affinity.Model(
            Affinity.stats(active), Affinity.seedProfile(store.seedArtistCounts()), Affinity.idf(pool), active.size,
        )
    }

    private fun funnel(
        total: Int, blocked: Int, quality: Map<String, Int>, qualityDropped: Int, recencyDropped: Int,
        lane: Map<String, Int>, served: Int,
    ): Map<String, Any> = linkedMapOf(
        "candidates" to total,
        "rejected_blocked" to blocked,
        "rejected_quality" to qualityDropped,
        "quality_detail" to quality,
        "rejected_recency_filter" to recencyDropped,
        "lane_detail" to lane,
        "eligible" to served,
    )

    /** Audit funnel of the last pool build (Debug screen / `/stats`). */
    @Synchronized
    fun funnelReport(): Map<String, Any> {
        ensurePool(dials = sessionDials)
        @Suppress("UNCHECKED_CAST")
        return (lastFilterStats["funnel"] as? Map<String, Any> ?: emptyMap()) +
            mapOf("refill_last" to (store.meta("refill_last") ?: ""), "queue" to queue.status())
    }

    // ---- supply: on-device refill (Last.fm + MusicBrainz, one rate-limited queue) ----

    /**
     * Pull new candidates for liked tracks/artists. Network runs on the caller's
     * thread (call from a background dispatcher); nothing here touches Google.
     */
    fun refill(maxNew: Int = 80): Refill.Report {
        val (tracks, artists) = synchronized(this) {
            val active = personalJudgments().filter { Affinity.UTILITY[it.rating]?.let { u -> u > 0 } == true }
            val byKey = store.ratings(100_000).associateBy { it.key }
            val t = active.asReversed().mapNotNull { j -> byKey[j.key]?.let { it.artist to it.title } }.take(25)
            val a = (t.map { it.first } + store.seedArtistCounts().entries.sortedByDescending { it.value }.map { it.key })
                .distinctBy { Affinity.artistKey(it) }.take(15)
            t to a
        }
        val rep = refiller.run(lastfmApiKey.trim(), tracks, artists, maxNew)
        synchronized(this) { builtFor = null }
        return rep
    }

    // ---- friends mode ----

    data class FriendsCard(val rec: Recommendation, val personal: Double, val broad: Double, val audience: Double)

    private fun listenerRef(kind: String): List<Double> =
        store.metaWithPrefix("lfm_listeners:$kind:").values.mapNotNull { it.toDoubleOrNull() }.sorted()

    private fun broadAppealOf(artist: String, title: String, tags: List<String>, refT: List<Double>, refA: List<Double>): Double {
        val t = store.meta("lfm_listeners:track:${normalizeKey(artist, title)}")?.toDoubleOrNull()
        val a = store.meta("lfm_listeners:artist:${Affinity.artistKey(artist)}")?.toDoubleOrNull()
        return BroadAppeal.appeal(t, a, tags, refT, refA)
    }

    private fun profileJudgments(profile: String): List<Affinity.Judgment> {
        val rows = store.feedbackRows("profile_judgment").mapNotNull { r ->
            val d = runCatching { kotlinx.serialization.json.Json.parseToJsonElement(r.detail) as JsonObject }.getOrNull() ?: return@mapNotNull null
            if ((d["profile"] as? JsonPrimitive)?.content != profile) return@mapNotNull null
            val rating = (d["rating"] as? JsonPrimitive)?.content ?: return@mapNotNull null
            val feats = (d["features"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.content }
                ?: Affinity.features(r.artist, emptyList(), "", null)
            Affinity.Judgment(r.key, rating, feats)
        }
        return Affinity.activeJudgments(rows, if (profile == "audience:group") AUDIENCE_UTILITY else Affinity.UTILITY)
    }

    /**
     * Friends playback queue: my Loved/Liked tracks (+ my YouTube likes at A=0.5)
     * whose broad appeal B ≥ 0.70, ranked by A·B. No personal-model changes.
     */
    @Synchronized
    fun friendsQueue(limit: Int = 30, minB: Double = BroadAppeal.MIN_B): List<FriendsCard> {
        val refT = listenerRef("track")
        val refA = listenerRef("artist")
        val aud = Affinity.stats(profileJudgments("audience:group"), AUDIENCE_UTILITY)
        val seen = HashSet<String>()
        val out = ArrayList<FriendsCard>()
        val byKey = store.ratings(100_000).associateBy { it.key }
        for (j in personalJudgments()) {
            val a = when (j.rating) { "love" -> 1.0; "like" -> 0.5; else -> continue }
            val r = byKey[j.key] ?: continue
            seen += j.key
            val tags = j.features.filter { it.startsWith("tag:") }.map { it.removePrefix("tag:") }
            val b = broadAppealOf(r.artist, r.title, tags, refT, refA)
            if (b >= minB) out += friendsCard(r.artist, r.title, r.cluster, tags, a, b, aud, j.features)
        }
        for (s in store.likedSeeds()) {
            if (s.holdout || !seen.add(s.key)) continue
            val b = broadAppealOf(s.artist, s.title, emptyList(), refT, refA)
            if (b >= minB) out += friendsCard(s.artist, s.title, "", emptyList(), 0.5, b, aud, Affinity.features(s.artist, emptyList(), "", null), s.videoId)
        }
        return out.sortedByDescending { it.personal * it.broad }.take(limit)
    }

    private fun friendsCard(
        artist: String, title: String, cluster: String, tags: List<String>, a: Double, b: Double,
        aud: Affinity.Stats, feats: List<String>, videoId: String = "",
    ): FriendsCard {
        val audScore = 0.6 * aud.family("tag", feats) + 0.4 * aud.family("artist", feats)
        val vid = resolver.resolve(artist, title, videoId, allowNetworkLookup = false).let { if (it.ok && it.videoId.length == 11) it.videoId else "" }
        val search = YouTubeIds.searchUrl(artist, title)
        val rec = Recommendation(
            artist = artist, title = title, lane = "friends", confidence = a * b, cluster = cluster, tags = tags,
            why = "Your ${if (a >= 1.0) "Love" else "Like"} · broad appeal ${"%.2f".format(b)}",
            ytmUrl = if (vid.isNotEmpty()) YouTubeIds.watchUrl(vid) else search, ytmSearchUrl = search,
            ytmIntent = YouTubeIds.intentUri(artist, title, vid), ytmHandoff = if (vid.isNotEmpty()) "watch" else "search_fallback",
            videoId = vid, matchLabel = "Crowd-pleaser", laneLabel = "friends", broadAppeal = b,
        )
        return FriendsCard(rec, a, b, audScore)
    }

    /**
     * Friends discovery/audition queue: unrated candidates ranked by
     * 0.6·B + 0.4·(audience family + 1)/2 — the group profile, never the personal one.
     */
    @Synchronized
    fun friendsAudition(limit: Int = 20): List<FriendsCard> {
        val refT = listenerRef("track")
        val refA = listenerRef("artist")
        val aud = Affinity.stats(profileJudgments("audience:group"), AUDIENCE_UTILITY)
        val rated = store.ratings(100_000).map { it.key }.toSet()
        val blocked = store.blockedKeys()
        return (store.candidates("proven") + store.candidates("fresh"))
            .filter { it.key() !in rated && it.key() !in blocked }
            .map { c ->
                val f = Affinity.features(c)
                val b = broadAppealOf(c.artist, c.title, c.tags, refT, refA)
                val audF = 0.6 * aud.family("tag", f) + 0.4 * aud.family("artist", f)
                val card = friendsCard(c.artist, c.title, c.cluster, c.tags, 0.0, b, aud, f)
                card.copy(rec = card.rec.copy(lane = "audition", laneLabel = "audition", matchLabel = "Audition",
                    why = "Broad appeal ${"%.2f".format(b)} · group fit ${"%.2f".format(audF)}",
                    confidence = 0.6 * b + 0.4 * (audF + 1) / 2), audience = audF)
            }
            .sortedByDescending { it.rec.confidence }
            .take(limit)
    }

    private fun addProfileJudgment(profile: String, rec: Recommendation, rating: String, reaction: String?): Long {
        val key = normalizeKey(rec.artist, rec.title)
        val detail = buildJsonObject {
            put("profile", profile); put("rating", rating); reaction?.let { put("reaction", it) }
            put("features", JsonArray(Affinity.features(rec.artist, rec.tags, rec.cluster, null).map { JsonPrimitive(it) }))
        }
        val id = store.addFeedback(key, "profile_judgment", rec.artist, rec.title, detail.toString(), rec.lane, clock())
        pushUndo(UndoEntry("profile", rec, null, id, emptyList(), emptyList(), "", null))
        return id
    }

    /** Group reaction (Liked / Didn't work / Mixed) → audience:group only. No personal transfer. */
    @Synchronized
    fun groupReact(rec: Recommendation, reaction: String): Long {
        require(reaction in BroadAppeal.REACTION_UTILITY) { "reaction must be liked|didnt_work|mixed" }
        return addProfileJudgment("audience:group", rec, "group_$reaction", reaction)
    }

    /** My own reaction while in friends context → personal_friends_context only. */
    @Synchronized
    fun friendsContextRate(rec: Recommendation, rating: String): Long {
        require(rating in Affinity.UTILITY) { "rating must be love|like|meh|dislike|skip" }
        return addProfileJudgment("personal_friends_context", rec, rating, null)
    }

    /** Explicit "also teach my personal recs": a normal (undoable) personal rating. */
    fun teachPersonal(rec: Recommendation, rating: String) =
        rate(rec.artist, rec.title, rating, lane = if (rec.lane == "fresh") "fresh" else "proven", cluster = rec.cluster, shown = rec)

    @Synchronized
    fun profileSizes(): Map<String, Int> = mapOf(
        "personal" to personalJudgments().size,
        "personal_friends_context" to profileJudgments("personal_friends_context").size,
        "audience:group" to profileJudgments("audience:group").size,
    )

    @Synchronized
    fun lessLike(factor: String, lane: String = "", cluster: String = "other") {
        learning.lessLike(factor, lane, cluster)
        store.addFeedback("", "less_like", "", "", factor, lane, clock())
    }

    fun featuresFor(key: String, lane: String): Map<String, Double> {
        val f = LinkedHashMap<String, Double>()
        f["artist_overlap"] = 0.5
        if (lane == "fresh") { f["freshness"] = 1.0; f["deezer_fresh"] = 0.5 } else { f["deezer"] = 0.5; f["listenbrainz"] = 0.3 }
        store.lastLog(key)?.metricsSnapshot?.takeIf { it.isNotEmpty() }?.let { snap ->
            runCatching {
                Json.parseToJsonElement(snap).jsonObject.forEach { (name, m) ->
                    val norm = ((m as? JsonObject)?.get("norm") as? JsonPrimitive)?.doubleOrNull
                    if (norm != null) f["m_$name"] = norm
                }
            }
        }
        store.metricsFor(key)?.forEach { (name, m) -> m.norm?.let { f["m_$name"] = it } }
        return f
    }

    /** `GET /dials` */
    @Synchronized
    fun dials(): DialState = DialState(
        lane = sessionDials.lane,
        preset = sessionDials.preset,
        availableTagDials = vocab.dialTags(),
        presets = SeedImporter.presets(store).keys.toList(),
        availableGenres = vocab.availableGenreChips(),
        enabledGenres = sessionDials.enabledGenres?.sorted(),
    )

    /** `POST /dials` (enabled_genres always explicit here: null = all chips on). */
    @Synchronized
    fun setDials(lane: String, preset: String = "", enabledGenres: List<String>?): DialState {
        var d = DialSet(lane = lane.ifEmpty { "auto" }, preset = preset)
        if (preset.isNotEmpty()) d = runCatching { d.applyPreset(preset, SeedImporter.presets(store)) }.getOrDefault(d)
        d = d.copy(enabledGenres = enabledGenres?.map { it.trim() }?.filter { it.isNotEmpty() }?.toSet())
        sessionDials = d
        return dials()
    }

    /** `POST /correct_video`: permanent "wrong song" override (applied after auto resolve). */
    @Synchronized
    fun correctVideo(artist: String, title: String, videoIdOrUrl: String): ResolveResult =
        resolver.recordCorrection(artist, title, videoIdOrUrl)

    /** Refit Platt calibration from rated predictions (CLI `calibrate` on the PC). */
    @Synchronized
    fun recalibrate(): Calibration.Calib {
        val pairs = store.ratings(100_000).mapNotNull { r -> r.predictedConfidence?.let { it to r.rating } }
        val c = Calibration.fit(pairs)
        store.saveCalibration(c, clock())
        builtFor = null
        return c
    }

    /** `GET /export` (same shape as the PC engine, plus a format marker). */
    @Synchronized
    fun exportJson(): String = buildJsonObject {
        put("format", "musicfinder-export")
        put("engine", "android-local ${EngineConfig.VERSION}")
        put("exported_at", clock())
        put("ratings", buildJsonArray {
            store.ratings(500).forEach { r ->
                add(buildJsonObject {
                    put("artist", r.artist); put("title", r.title); put("key", r.key); put("rating", r.rating)
                    put("lane", r.lane); put("cluster", r.cluster); put("predicted_confidence", r.predictedConfidence)
                    put("created_at", r.createdAt)
                })
            }
        })
        val d = dials()
        put("dials", buildJsonObject {
            put("lane", d.lane); put("preset", d.preset)
            put("enabled_genres", d.enabledGenres?.let { JsonArray(it.map { g -> JsonPrimitive(g) }) } ?: JsonNull)
            put("available_genres", JsonArray(d.availableGenres.map { JsonPrimitive(it) }))
            put("genre_match", "or_include_enabled_casefold_tag_or_cluster")
        })
        put("tags", buildJsonArray {
            store.listTags(limit = 500).forEach { t ->
                add(buildJsonObject {
                    put("tag", t.tag); put("kind", t.kind); put("count", t.count); put("as_dial", if (t.asDial) 1 else 0)
                    put("first_seen", t.firstSeen); put("last_seen", t.lastSeen)
                })
            }
        })
    }.toString()

    @Synchronized
    fun stats(): Map<String, Any?> = mapOf(
        "seed_version" to store.meta("seed_version"),
        "pool" to store.candidateCounts(),
        "pool_ready" to mapOf("proven" to cacheProven.size, "fresh" to cacheFresh.size),
        "ratings" to store.ratings(100_000).size,
        "recommendations_logged" to store.logCount(),
        "weights" to store.weightCount(),
        "calibration" to store.latestCalibration()?.kind,
        "last_filter_stats" to lastFilterStats,
        "fresh_min_confidence" to freshMinConfidence,
        "video_ids" to videoCoverage(),
        "session_id" to sessionId,
        "model_version" to modelVersion,
        "fresh_days" to (freshExpandDays ?: EngineConfig.FRESH_DAYS),
        "last_exhaustion" to lastExhaustion,
        "lastfm_key_set" to lastfmApiKey.isNotBlank(),
        "refill_last" to store.meta("refill_last"),
        "request_queue" to queue.status(),
        "profiles" to profileSizes(),
    )

    /** How many pool candidates open as an exact video right now (local data only). */
    fun videoCoverage(): Map<String, Int> {
        var ok = 0; var neg = 0; var total = 0
        for (lane in listOf("proven", "fresh")) for (c in store.candidates(lane)) {
            total++
            val r = resolver.resolve(c.artist, c.title, candVideoId(c), allowNetworkLookup = false)
            if (r.ok) ok++ else if (resolver.isNegativeCached(c.key())) neg++
        }
        return mapOf("resolved" to ok, "known_no_video" to neg, "total" to total)
    }
}
