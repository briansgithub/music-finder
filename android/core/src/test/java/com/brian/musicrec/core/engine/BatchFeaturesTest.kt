package com.brian.musicrec.core.engine

import com.brian.musicrec.core.Recommendation
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate
import java.util.concurrent.atomic.AtomicInteger
import java.util.zip.GZIPInputStream
import kotlin.random.Random

/** not_now → lane auto, fresh min confidence, Dislike (+undo/export/import), video-ID resolve caching & timeouts, seed merge. */
class BatchFeaturesTest {
    private val bundle = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream()).bufferedReader().readText()
    private var now = 1_800_000_000.0

    private fun engine(store: InMemoryEngineStore = InMemoryEngineStore(), net: NetFetcher? = null, online: Boolean = false, seed: Boolean = true): LocalEngine {
        val e = LocalEngine(
            store, net = net, allowNetworkResolve = { online }, random = Random(11),
            clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) },
        )
        if (seed) e.importText(bundle)
        return e
    }

    private fun LocalEngine.rateShown(r: Recommendation, rating: String) =
        rate(r.artist, r.title, rating, r.lane, r.cluster, r.confidence, shown = r)

    // ---- 2. not_now never leaves a sticky lane ----
    @Test fun notNow_resetsLaneToAuto() {
        val e = engine()
        e.setDials("fresh", "", null)
        assertEquals("fresh", e.dials().lane)
        val r = e.next("fresh")!!
        e.notNow(r.artist, r.title, r.lane, shown = r)
        assertEquals("auto", e.dials().lane)
        // with lane=auto most cards come from proven again
        val lanes = (1..12).mapNotNull { e.next("auto")?.lane }
        assertTrue("lanes=$lanes", lanes.count { it == "proven" } >= lanes.size / 2)
    }

    // ---- 3. fresh lane minimum confidence ----
    @Test fun freshLane_requiresMinConfidence_elseFallsBackToProven() {
        val e = engine()
        e.freshMinConfidence = 0.99
        // Batch 2: explicit fresh mode reports exhaustion instead of silently serving proven…
        assertNull(e.next("fresh"))
        assertEquals("fresh", e.lastExhaustion!!["lane"])
        assertEquals(true, e.lastExhaustion!!["can_expand"])
        // …while auto falls back to proven.
        assertEquals("proven", e.next()!!.lane)
        @Suppress("UNCHECKED_CAST")
        val fs = e.lastFilterStats["fresh"] as Map<String, Int>
        assertEquals(0, fs["scored"])
        assertTrue(fs["low_confidence_rejects"]!! >= 0)

        val lo = engine()
        lo.freshMinConfidence = 0.0
        val f = lo.next("fresh")!!
        assertEquals("fresh", f.lane)

        val d = engine() // default 0.7: every fresh card served meets it
        assertEquals(EngineConfig.FRESH_MIN_CONFIDENCE, d.freshMinConfidence, 0.0)
        repeat(6) { d.next("fresh")?.let { c -> if (c.lane == "fresh") assertTrue(c.confidence >= 0.7) } }
    }

    // ---- 4. Dislike ----
    @Test fun dislike_isStrongNegative_withArtistDownWeight_notBan_andUndoable() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val r = e.next()!!
        val ak = Learning.artistKey(r.artist)
        val before = store.allWeights()
        e.rateShown(r, "dislike")
        assertEquals("dislike", store.ratings(1).first().rating)
        assertEquals(-EngineConfig.ARTIST_DISLIKE_STEP, store.getWeight("artist", ak)!!.weight, 1e-12)
        assertEquals(mapOf("artist:$ak" to -EngineConfig.ARTIST_DISLIKE_STEP), e.learning.artistWeights())

        // stronger than skip on the shared features (DISLIKE_STRENGTH × the skip update)
        val s1 = InMemoryEngineStore(); val s2 = InMemoryEngineStore()
        val l1 = Learning(s1) { 1.0 }; val l2 = Learning(s2) { 1.0 }
        l1.updateFromRating(mapOf("deezer" to 0.5), "skip", "proven", "pop")
        l2.updateFromRating(mapOf("deezer" to 0.5), "dislike", "proven", "pop")
        assertTrue(s2.getWeight("global", "deezer")!!.beta > s1.getWeight("global", "deezer")!!.beta)

        // floor, never a ban: score penalty only
        repeat(5) { e.learning.updateArtistAffinity(r.artist, "dislike") }
        assertEquals(EngineConfig.ARTIST_MIN_WEIGHT, store.getWeight("artist", ak)!!.weight, 1e-12)
        val c = Candidate(r.artist, "Another Song", sourceSignals = linkedMapOf("deezer" to 1.0, "artist_overlap" to 0.6))
        val c2 = c.copy(sourceSignals = LinkedHashMap(c.sourceSignals))
        val w = e.learning.sampleWeights("proven", "other", Random(1), explore = false)
        Scorer.score(c, w, "proven", today = LocalDate.of(2026, 11, 6))
        Scorer.score(c2, w + e.learning.artistWeights(), "proven", today = LocalDate.of(2026, 11, 6))
        assertEquals(c.score - (-EngineConfig.ARTIST_MIN_WEIGHT * EngineConfig.ARTIST_PENALTY_SCALE), c2.score, 1e-9)
        assertTrue(c2.whyParts.any { it.feature == "artist_dislike" })
        // like/love recover toward 0
        e.learning.updateArtistAffinity(r.artist, "love")
        assertEquals(EngineConfig.ARTIST_MIN_WEIGHT + EngineConfig.ARTIST_RECOVER_STEP, store.getWeight("artist", ak)!!.weight, 1e-12)
    }

    @Test fun dislike_undo_restoresExactly() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val r = e.next()!!
        val before = store.allWeights()
        val ratingsBefore = store.ratings(100_000)
        e.rateShown(r, "dislike")
        val res = e.undo()!!
        assertEquals(r, res.rec)
        assertEquals(before, store.allWeights())
        assertEquals(ratingsBefore, store.ratings(100_000))
        assertNull(store.getWeight("artist", Learning.artistKey(r.artist)))
    }

    @Test fun dislike_exportImport_roundTrip_replaysArtistDownWeight() {
        val e = engine()
        val r = e.next()!!
        e.rateShown(r, "dislike")
        val exported = e.exportJson()
        val ratings = Json.parseToJsonElement(exported).jsonObject["ratings"]!!.jsonArray
        assertTrue(ratings.any { it.jsonObject["rating"]!!.jsonPrimitive.content == "dislike" })
        val store2 = InMemoryEngineStore()
        val e2 = engine(store2)
        val s = e2.importText(exported)
        assertEquals(1, s.ratingsAdded)
        assertEquals("dislike", store2.ratings(1).first().rating)
        assertTrue(store2.getWeight("artist", Learning.artistKey(r.artist))!!.weight < 0)
    }

    // ---- 1. video IDs: bundled, negative cache, short timeout, background upgrade ----
    @Test fun bundledVideoIds_resolveOffline() {
        val e = engine()
        val cov = e.videoCoverage()
        assertTrue("coverage=$cov", cov["total"]!! > 0)
        // every bundled candidate carrying an ID opens as an exact video with no network
        val withId = e.store.candidates("proven").filter { it.externalIds["youtube"]?.length == 11 }
        for (c in withId.take(20)) {
            val res = e.resolver.resolve(c.artist, c.title, c.externalIds["youtube"]!!, allowNetworkLookup = false)
            assertTrue(res.ok)
        }
    }

    @Test fun negativeResult_isCached_noRepeatLookups() {
        val calls = AtomicInteger()
        val net = NetFetcher { _, _ -> calls.incrementAndGet(); """{"recordings":[]}""" }
        val e = engine(net = net, online = true)
        assertTrue(e.resolver.canLookup("Nobody Known", "No Such Tune"))
        assertFalse(e.resolver.resolve("Nobody Known", "No Such Tune").ok)
        val n = calls.get()
        assertTrue(n >= 1)
        assertFalse(e.resolver.canLookup("Nobody Known", "No Such Tune"))
        val again = e.resolver.resolve("Nobody Known", "No Such Tune")
        assertFalse(again.ok)
        assertTrue(again.chain.contains("negative_cache"))
        assertEquals(n, calls.get())
    }

    @Test fun seedNegatives_imported_andMergeKeepsRatings() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val r = e.next()!!
        e.rateShown(r, "like")
        val root = Json.parseToJsonElement(bundle).jsonObject
        val negKey = normalizeKey("Neg Artist", "Neg Song")
        val newer = JsonObject(root + mapOf(
            "exported_at" to JsonPrimitive(1_900_000_000.5),
            "yt_play_resolve_negative" to buildJsonArray {
                add(buildJsonObject { put("key", negKey); put("status", "no_match"); put("updated_at", now) })
            },
        ))
        val s = e.importText(newer.toString())
        assertEquals(1, s.negatives)
        assertTrue(e.resolver.isNegativeCached(negKey))
        assertEquals("1.9000000005E9".toDouble(), e.seedExportedRaw()!!.toDouble(), 0.0)
        assertEquals("like", store.ratings(100_000).first { it.key == normalizeKey(r.artist, r.title) }.rating)
        assertEquals(0, s.ratingsAdded) // nothing duplicated
    }

    @Test fun next_isNotBlockedBySlowLookup_andUpgradesInBackground() {
        val net = NetFetcher { url, _ ->
            Thread.sleep(1500)
            if ("inc=url-rels" in url) """{"relations":[{"url":{"resource":"https://www.youtube.com/watch?v=AbCdEfGhIjK"}}]}"""
            else """{"recordings":[{"id":"mbid-1","title":"%s"}]}"""
        }
        val store = InMemoryEngineStore()
        val e = engine(store, net = null, seed = true) // pool without IDs needed: use a fresh store engine with net
        val e2 = LocalEngine(store, net = NetFetcher { url, h ->
            // title-echo so the MB title check passes for whatever card comes up
            val t = Regex("recording%3A%22(.+?)%22").find(url)?.groupValues?.get(1)?.let { java.net.URLDecoder.decode(it, "UTF-8") } ?: ""
            net.get(url, h).replace("%s", t.replace("\"", ""))
        }, allowNetworkResolve = { true }, random = Random(3), clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) })
        e2.resolveWaitMs = 300
        // make sure the next card has no local ID: strip IDs from the pool
        val stripped = listOf("proven", "fresh").associateWith { lane ->
            // new keys (no bundled/pre-resolved ID, no negative cache) with the same signals
            store.candidates(lane).map { it.copy(title = it.title + " Zq", externalIds = LinkedHashMap(it.externalIds - "youtube"), youtubeUrl = "") }
        }
        store.clearCandidates()
        stripped.forEach { (lane, cs) -> cs.forEach { store.putCandidate(lane, it) } }
        val t0 = System.nanoTime()
        var r: Recommendation? = null
        // find a card that isn't resolvable locally
        val seen = ArrayList<String>()
        for (i in 0 until 10) {
            val c = e2.next()
            seen += "${c?.artist}|${c?.ytmHandoff}|${c?.resolveSource}"
            if (c != null && c.ytmHandoff != "watch") { r = c; break }
        }
        println("bg-test cards: $seen pool=${store.candidateCounts()}")
        val first = r
        assertNotNull("expected at least one card without a local video ID", first); first!!
        val ms = (System.nanoTime() - t0) / 1_000_000
        assertEquals("search_fallback", first.ytmHandoff)
        assertTrue("next() took $ms ms", ms < 10 * 1000)
        val up = e2.awaitVideo(first, 15_000)
        assertNotNull(up)
        assertEquals("watch", up!!.ytmHandoff)
        assertEquals("AbCdEfGhIjK", up.videoId)
        assertTrue(up.resolveSource, "musicbrainz_url" in up.resolveSource)
        @Suppress("UNUSED_VARIABLE") val unused = e
    }
}
