package com.brian.musicrec.core.engine

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.double
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import java.io.File
import java.time.LocalDate
import java.util.zip.GZIPInputStream
import kotlin.math.abs

/**
 * Compares the Kotlin port against reference outputs produced by the Python
 * engine (`recommender/scripts/make_parity_fixtures.py`) on the same seed data.
 */
class ParityTest {
    companion object {
        lateinit var fx: JsonObject
        lateinit var bundleText: String
        lateinit var today: LocalDate

        @BeforeClass @JvmStatic
        fun load() {
            fx = Json.parseToJsonElement(
                ParityTest::class.java.getResource("/parity/fixtures.json")!!.readText(),
            ).jsonObject
            bundleText = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream())
                .bufferedReader().readText()
            today = LocalDate.parse(fx["today"]!!.jsonPrimitive.content)
        }

        fun freshStore(): InMemoryEngineStore {
            val s = InMemoryEngineStore()
            SeedImporter(s).importText(bundleText, 1.0)
            Learning(s) { 1.0 }.ensurePriors()
            return s
        }

        const val EPS = 1e-9
    }

    private fun JsonElement.obj() = this.jsonObject
    private fun JsonElement.d() = this.jsonPrimitive.double
    private fun JsonElement.s() = this.jsonPrimitive.content
    private fun near(exp: Double, act: Double, what: String, eps: Double = EPS) =
        assertTrue("$what: expected $exp got $act", abs(exp - act) <= eps)

    private fun weights(name: String): Map<String, Double> =
        fx["weights"]!!.obj()[name]!!.obj().mapValues { it.value.d() }

    @Test fun normalizeKey_matchesPython() {
        for (c in fx["normalize_key"]!!.jsonArray) {
            val o = c.obj()
            assertEquals(o["key"]!!.s(), normalizeKey(o["artist"]!!.s(), o["title"]!!.s()))
        }
    }

    @Test fun classifyTitle_matchesPython() {
        for (c in fx["classify_title"]!!.jsonArray) {
            val o = c.obj()
            val r = TitleFilters.classify(o["title"]!!.s())
            assertEquals("action for '${o["title"]!!.s()}'", o["action"]!!.s(), r.action)
            assertEquals("reasons for '${o["title"]!!.s()}'", o["reasons"]!!.jsonArray.map { it.s() }, r.reasons)
        }
    }

    @Test fun sanitize_clusters_reissue_matchPython() {
        for (c in fx["sanitize"]!!.jsonArray) {
            val o = c.obj(); val n = o["name"]!!.s()
            assertEquals("plausible $n", o["plausible"]!!.jsonPrimitive.boolean, Sanitize.isPlausibleArtist(n))
            assertEquals("vocab $n", o["vocab"]!!.jsonPrimitive.boolean, Sanitize.isVocabEligible(n))
            assertEquals("kind $n", o["kind"]!!.s(), Sanitize.inferTagKind(n))
            assertEquals("dial $n", o["dial"]!!.jsonPrimitive.boolean, Sanitize.isDialWorthy(n))
        }
        for (c in fx["clusters"]!!.jsonArray) {
            val o = c.obj()
            assertEquals(o["cluster"]!!.s(), Clusters.assign(o["tags"]!!.jsonArray.map { it.s() }, o["artist"]!!.s(), o["title"]!!.s()))
        }
        for (c in fx["reissue"]!!.jsonArray) {
            val o = c.obj()
            assertEquals(
                "reissue ${o["title"]!!.s()}", o["result"]!!.jsonPrimitive.boolean,
                Reissues.isReissueOrOld(o["title"]!!.s(), o["album"]!!.s(), o["release_date"]!!.s(), null, 30, today),
            )
        }
    }

    @Test fun confidence_calibration_penalty_dials_matchPython() {
        for (c in fx["raw_confidence"]!!.jsonArray) {
            val o = c.obj()
            val sig = LinkedHashMap(o["signals"]!!.obj().mapValues { it.value.d() })
            near(o["result"]!!.d(), Calibration.rawConfidenceFromSignals(sig, o["lane"]!!.s()), "raw_conf $sig")
        }
        for (c in fx["calibration"]!!.jsonArray) {
            val o = c.obj()
            val cal = (o["calib"] as? JsonObject)?.let { Calibration.Calib(it["kind"]!!.s(), it["a"]!!.d(), it["b"]!!.d()) }
            near(o["result"]!!.d(), Calibration.apply(o["p"]!!.d(), cal), "calib ${o["p"]}", 0.0)
        }
        for (c in fx["mainstream"]!!.jsonArray) {
            val o = c.obj()
            val pop = (o["popularity"] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.double
            near(o["penalty"]!!.d(), Originality.mainstreamPenalty(Candidate("a", "b", popularity = pop)), "penalty $pop")
        }
        val dj = fx["dials"]!!.obj()
        val m = JsonCodec.metrics(dj["metrics"])
        val ds = DialSet(
            filters = mapOf("energy" to (0.0 to 0.5), "tempo_bpm" to (60.0 to 110.0), "freshness_days" to (0.0 to 7.0)),
            targets = linkedMapOf("energy" to (0.3 to 1.0), "tempo_bpm" to (100.0 to 2.0), "missing" to (0.5 to 1.0)),
        )
        assertEquals(dj["passes"]!!.jsonPrimitive.boolean, ds.passesFilters(m).first)
        near(dj["target_score"]!!.d(), ds.targetScore(m), "target_score")
        assertEquals(dj["fail_passes"]!!.jsonPrimitive.boolean, DialSet(filters = mapOf("energy" to (0.6 to 1.0))).passesFilters(m).first)
    }

    @Test fun learnedWeights_posteriorMean_matchPython() {
        val store = freshStore()
        val l = Learning(store) { 1.0 }
        val rnd = kotlin.random.Random(1)
        for ((name, lane, cluster) in listOf(
            Triple("proven_other", "proven", "other"), Triple("fresh_other", "fresh", "other"),
            Triple("fresh_electronic", "fresh", "electronic"),
        )) {
            val exp = weights(name)
            val act = l.sampleWeights(lane, cluster, rnd, explore = false)
            assertEquals(exp.keys.toList(), act.keys.toList())
            exp.forEach { (k, v) -> near(v, act[k]!!, "$name.$k") }
        }
    }

    @Test fun scoreCandidate_matchesPython() {
        val variants = fx["metric_variants"]!!.jsonArray
        val dialsT = DialSet(targets = linkedMapOf("energy" to (0.7 to 1.0), "tempo_bpm" to (120.0 to 0.5)))
        var n = 0
        for (c in fx["score_cases"]!!.jsonArray) {
            val o = c.obj()
            val cand = JsonCodec.candidate(o["input"]!!.obj())
            val lane = o["lane"]!!.s()
            val opts = o["opts"]!!.obj()
            val metrics = LinkedHashMap<String, MetricValue>()
            val v = variants[o["metric_variant"]!!.jsonPrimitive.content.toInt()]
            if (v !is JsonNull) metrics.putAll(JsonCodec.metrics(v))
            metrics.putAll(Lanes.contextMetrics(cand, today))
            Scorer.score(
                cand, weights(o["weights"]!!.s()), lane,
                obscurity = opts["obscurity"]?.d() ?: 0.0,
                adventurous = opts["adventurous"]?.jsonPrimitive?.boolean ?: false,
                genre = opts["genre"]?.s() ?: "",
                dials = if (o["dials_targets"]!!.jsonPrimitive.boolean) dialsT else null,
                calib = null, metricsIn = metrics, today = today,
            )
            assertCand(o["expected"]!!.obj(), cand, "case $n")
            n++
        }
        assertTrue("expected fixture cases", n >= 30)
    }

    private fun assertCand(e: JsonObject, c: Candidate, what: String) {
        assertEquals("$what key", e["key"]!!.s(), c.key())
        near(e["score"]!!.d(), c.score, "$what score")
        near(e["confidence"]!!.d(), c.confidence, "$what confidence", 0.0)
        assertEquals("$what why", e["why"]!!.s(), c.why)
        val parts = e["why_parts"]!!.jsonArray.map { it.obj() }
        assertEquals("$what why_parts size", parts.size, c.whyParts.size)
        parts.zip(c.whyParts).forEach { (p, k) ->
            assertEquals("$what feature", p["feature"]!!.s(), k.feature)
            near(p["value"]!!.d(), k.value, "$what ${k.feature}.value")
            near(p["weight"]!!.d(), k.weight, "$what ${k.feature}.weight")
            near(p["contrib"]!!.d(), k.contrib, "$what ${k.feature}.contrib")
        }
        assertEquals("$what tags", e["tags"]!!.jsonArray.map { it.s() }, c.tags)
        assertEquals("$what cluster", e["cluster"]!!.s(), c.cluster)
    }

    private fun rankAndCompare(fixture: JsonObject, enabled: Set<String>?, session: SessionMemory?, label: String) {
        val store = freshStore()
        val qp = CandidateQuality.apply(store.candidates("proven"), session, enabled)
        val qf = CandidateQuality.apply(store.candidates("fresh"), session, enabled)
        assertEquals("$label quality proven", fixture["quality_proven"]!!.obj().mapValues { it.value.d().toInt() }, qp.stats.toMap())
        assertEquals("$label quality fresh", fixture["quality_fresh"]!!.obj().mapValues { it.value.d().toInt() }, qf.stats.toMap())
        val (sp, _) = Lanes.selectProven(store, qp.kept, weights("proven_other"), calib = null, today = today)
        val (sf, _) = Lanes.selectFresh(store, qf.kept, weights("fresh_other"), calib = null, today = today)
        for ((name, got) in listOf("proven" to sp, "fresh" to sf)) {
            val exp = fixture[name]!!.jsonArray.map { it.obj() }
            assertEquals("$label $name order", exp.map { it["key"]!!.s() }, got.map { it.key() })
            exp.zip(got).forEachIndexed { i, (e, c) -> assertCand(e, c, "$label $name[$i]") }
        }
    }

    @Test fun poolRanking_allChipsOn_matchesPython() = rankAndCompare(fx["rank_all"]!!.obj(), null, null, "all")

    @Test fun poolRanking_genreChips_orInclude_matchesPython() {
        val f = fx["rank_genres"]!!.obj()
        rankAndCompare(f, f["enabled"]!!.jsonArray.map { it.s() }.toSet(), null, "genres")
    }

    @Test fun poolRanking_noChipsOn_matchesPython() = rankAndCompare(fx["rank_none_enabled"]!!.obj(), emptySet(), null, "none")

    @Test fun poolRanking_sessionSuppress_matchesPython() {
        val s = SessionMemory()
        fx["session_negatives"]!!.jsonArray.forEach { val a = it.jsonArray; s.recordNegative(a[0].s(), a[1].s(), a[2].s()) }
        rankAndCompare(fx["rank_session"]!!.obj(), null, s, "session")
    }

    @Test fun learningUpdates_matchPython() {
        val store = freshStore()
        val l = Learning(store) { 2.0 }
        for (step in fx["learning_sequence"]!!.jsonArray) {
            val o = step.obj()
            l.updateFromRating(LinkedHashMap(o["features"]!!.obj().mapValues { it.value.d() }), o["rating"]!!.s(), o["lane"]!!.s(), o["cluster"]!!.s())
        }
        l.lessLike("energy", "proven", "other")
        for (r in fx["learning_after"]!!.jsonArray) {
            val o = r.obj()
            val w = store.getWeight(o["scope"]!!.s(), o["feature"]!!.s())!!
            val what = "${o["scope"]!!.s()}/${o["feature"]!!.s()}"
            near(o["alpha"]!!.d(), w.alpha, "$what alpha")
            near(o["beta"]!!.d(), w.beta, "$what beta")
            near(o["weight"]!!.d(), w.weight, "$what weight")
        }
        val exp = fx["weights_after_learning"]!!.obj()
        val act = l.sampleWeights("proven", "electronic", kotlin.random.Random(0), explore = false)
        exp.forEach { (k, v) -> near(v.d(), act[k]!!, "after.$k") }
    }

    @Test fun dislikeArtistAffinity_andPenalty_matchPython() {
        assertEquals(fx["fresh_min_confidence"]!!.d(), EngineConfig.FRESH_MIN_CONFIDENCE, 0.0)
        val store = freshStore()
        val l = Learning(store) { 2.0 }
        for (step in fx["artist_affinity"]!!.jsonArray) {
            val o = step.obj()
            l.updateArtistAffinity(o["artist"]!!.s(), o["rating"]!!.s())
            val exp = o["weight"]!!
            val act = store.getWeight("artist", Learning.artistKey(o["artist"]!!.s()))?.weight
            if (exp is JsonNull) assertEquals(null, act) else near(exp.d(), act!!, "affinity ${o["artist"]!!.s()} ${o["rating"]!!.s()}")
        }
        val aw = l.artistWeights()
        val expAw = fx["artist_weights"]!!.obj()
        assertEquals(expAw.keys, aw.keys)
        expAw.forEach { (k, v) -> near(v.d(), aw[k]!!, "aw.$k") }
        var n = 0
        for (c in fx["artist_penalty_cases"]!!.jsonArray) {
            val o = c.obj()
            val cand = JsonCodec.candidate(o["input"]!!.obj())
            Scorer.score(cand, weights("proven_other") + aw, "proven", calib = null, metricsIn = Lanes.contextMetrics(cand, today), today = today)
            assertCand(o["expected"]!!.obj(), cand, "penalty $n")
            n++
        }
        assertEquals(3, n)
    }

    @Test fun seedImport_keepsEveryCleanedCandidate() {
        val store = InMemoryEngineStore()
        val s = SeedImporter(store).importText(bundleText, 1.0)
        val b = Json.parseToJsonElement(bundleText).jsonObject["candidates"]!!.jsonObject
        assertEquals(b["proven"]!!.jsonArray.size + b["fresh"]!!.jsonArray.size, s.candidates)
        assertEquals(0, s.droppedCandidates)
        assertTrue(store.seedArtists().isNotEmpty())
    }
}
