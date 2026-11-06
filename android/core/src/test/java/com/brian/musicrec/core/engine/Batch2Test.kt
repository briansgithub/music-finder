package com.brian.musicrec.core.engine

import com.brian.musicrec.core.Recommendation
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate
import java.util.zip.GZIPInputStream
import kotlin.random.Random

/** Batch 2 (0.5.0): supply funnel/refill, shrinkage affinity, labels, friends mode, MMR diversity, logging. */
class Batch2Test {
    private val bundle = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream()).bufferedReader().readText()
    private var now = 1_800_000_000.0

    private fun engine(store: InMemoryEngineStore = InMemoryEngineStore(), net: NetFetcher? = null): LocalEngine {
        val e = LocalEngine(
            store, net = net, allowNetworkResolve = { false }, random = Random(5),
            clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) },
        )
        e.importText(bundle)
        return e
    }

    private fun LocalEngine.rateShown(r: Recommendation, rating: String) =
        rate(r.artist, r.title, rating, r.lane, r.cluster, r.confidence, shown = r)

    // ---- 1. supply: auditable funnel ----
    @Test fun funnel_accountsForEveryCandidate() {
        val e = engine()
        e.next()!!
        @Suppress("UNCHECKED_CAST")
        val f = e.funnelReport()
        for (lane in listOf("proven", "fresh")) {
            @Suppress("UNCHECKED_CAST")
            val m = f[lane] as Map<String, Any>
            val total = m["candidates"] as Int
            val lost = (m["rejected_blocked"] as Int) + (m["rejected_quality"] as Int) + (m["rejected_recency_filter"] as Int)
            @Suppress("UNCHECKED_CAST")
            val ld = m["lane_detail"] as Map<String, Int>
            val laneLost = ld.filterKeys { it.endsWith("_rejects") }.values.sum()
            assertEquals("$lane funnel adds up", total, lost + laneLost + ld["scored"]!!)
            assertTrue((m["eligible"] as Int) <= ld["scored"]!!)
        }
        assertNotNull(f["queue"])
    }

    // ---- 1. fresh is a cap (≤ 25 %) and needs to be within 0.15 of best proven ----
    @Test fun freshShare_isCapped_andGapGated() {
        val e = engine()
        e.freshMinConfidence = 0.0 // widest fresh pool
        val lanes = (1..24).mapNotNull { e.next()?.lane }
        val share = lanes.count { it == "fresh" }.toDouble() / lanes.size
        assertTrue("fresh share $share", share <= Diversity.FRESH_CAP + 1e-9)
        // lane choice unit: gap rule
        val p = Candidate("A", "x").apply { affinity = 0.6 }
        val fNear = Candidate("B", "y").apply { affinity = 0.5 }
        val fFar = Candidate("C", "z").apply { affinity = 0.3 }
        val hist = List(3) { Diversity.Served("k$it", "a$it", "proven", emptyMap()) }
        assertEquals("fresh", Diversity.chooseLane("auto", listOf(p), listOf(fNear), hist))
        assertEquals("proven", Diversity.chooseLane("auto", listOf(p), listOf(fFar), hist))
        // cap: 1 fresh of 2 served → next fresh would be 2/3 > 25 %
        val hist2 = listOf(Diversity.Served("k", "a", "fresh", emptyMap()), Diversity.Served("k2", "b", "proven", emptyMap()))
        assertEquals("proven", Diversity.chooseLane("auto", listOf(p), listOf(fNear), hist2))
    }

    // ---- 1. fresh mode exhaustion + labeled 90-day expansion ----
    @Test fun freshMode_reportsExhaustion_thenLabeledExpansion() {
        val e = engine()
        e.freshMinConfidence = 0.0
        var served = 0
        while (e.next("fresh") != null) served++
        val ex = e.lastExhaustion!!
        assertEquals("fresh", ex["lane"]); assertEquals(30, ex["fresh_days"]); assertEquals(true, ex["can_expand"])
        e.freshExpandDays = 90
        @Suppress("UNCHECKED_CAST")
        assertEquals(90, e.stats()["fresh_days"])
        e.next("fresh")?.let { assertEquals("fresh (90-day)", it.laneLabel) }
        assertTrue(served > 0)
    }

    // ---- 2/3. shrinkage stats, separate scores, exact undo ----
    @Test fun affinity_learnsFromRatings_undoExact_andLabelsUntil50() {
        val e = engine()
        val r = e.next()!!
        assertTrue(r.matchLabel in setOf("Strong match", "Good match", "Possible match", "Stretch", "Exploration pick"))
        assertFalse(r.matchLabel.contains("%"))
        assertTrue(r.selection != 0.0 || r.affinity != 0.0)
        val ak = Affinity.artistKey(r.artist)
        val before = e.stats()["profiles"]
        val p0 = (before as Map<*, *>)["personal"] as Int // the seed bundle carries the PC's ratings
        e.rateShown(r, "love")
        val n2 = e.next()!!
        assertEquals(p0 + 1, (e.stats()["profiles"] as Map<*, *>)["personal"])
        e.undo(displacedKey = normalizeKey(n2.artist, n2.title))
        assertEquals(before, e.stats()["profiles"])
        // one active judgment per recording: re-rating replaces
        e.rateShown(r, "like"); e.rateShown(r, "dislike")
        assertEquals(p0 + 1, (e.stats()["profiles"] as Map<*, *>)["personal"])
        // utilities: dislike → negative artist family
        val st = Affinity.stats(listOf(Affinity.Judgment("k", "dislike", listOf("artist:$ak"))))
        assertEquals(-1.0 / (1 + 5.0), st.family("artist", listOf("artist:$ak")), 1e-12)
        assertEquals("p(like) 63%", Affinity.matchLabel(0.0, 50, 0.63, false))
    }

    // ---- 5. MMR diversity: artist spacing, exploration every 5th, logging ----
    @Test fun mmr_spacing_exploration_andLogging() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val recs = (1..15).mapNotNull { e.next() }
        assertEquals(15, recs.size)
        for (i in recs.indices) {
            val window = recs.subList(maxOf(0, i - Diversity.ARTIST_SPACING + 1), i).map { Affinity.artistKey(it.artist) }
            if (Affinity.artistKey(recs[i].artist) in window) {
                // only allowed when spacing had to be relaxed — check via the log
                val log = Json.parseToJsonElement(store.lastLog(normalizeKey(recs[i].artist, recs[i].title))!!.metricsSnapshot).jsonObject["_log"]!!.jsonObject
                assertEquals("true", log["spacing_relaxed"]!!.jsonPrimitive.content)
            }
        }
        assertEquals("exploration", recs[4].slot); assertEquals("Exploration pick", recs[4].matchLabel)
        assertEquals("exploration", recs[9].slot)
        assertEquals("mmr", recs[0].slot)
        val log = Json.parseToJsonElement(store.lastLog(normalizeKey(recs[0].artist, recs[0].title))!!.metricsSnapshot).jsonObject["_log"]!!.jsonObject
        assertEquals(e.sessionId, log["session"]!!.jsonPrimitive.content)
        assertEquals(e.modelVersion, log["model"]!!.jsonPrimitive.content)
        for (k in listOf("affinity", "selection", "p_like", "mmr_penalty", "seed", "tags", "artist", "label")) assertNotNull(k, log[k])
    }

    // ---- 1. on-device refill: no key → skip Last.fm; one queue with UA, budget, backoff ----
    @Test fun refill_withoutKey_skipsLastfm_andUsesMusicBrainzFirstReleaseDate() {
        val calls = ArrayList<Pair<String, Map<String, String>>>()
        val net = NetFetcher { url, h ->
            calls += url to h
            when {
                "audioscrobbler" in url -> error("must not call Last.fm without a key")
                "/ws/2/artist?" in url -> {
                    val name = java.net.URLDecoder.decode(url.substringAfter("artist%3A%22").substringBefore("%22"), "UTF-8")
                    """{"artists":[{"id":"mbid-${name.length}","name":"$name"}]}"""
                }
                "/ws/2/recording?" in url && "firstreleasedate" in url ->
                    """{"recordings":[{"id":"r1","title":"Brand New Zq","first-release-date":"2026-10-20"},
                                      {"id":"r2","title":"Old Zq","first-release-date":"2019-01-01"}]}"""
                else -> error("unexpected $url")
            }
        }
        val e = engine(net = net)
        e.lastfmApiKey = ""
        val rep = e.refill()
        assertTrue(rep.notes.any { "Last.fm key not set" in it })
        val mb = rep.funnel["musicbrainz_fresh"]!!
        assertTrue("added ${rep.funnel}", (mb["added"] ?: 0) >= 1)
        assertTrue((mb["rejected_old_or_undated"] ?: 0) >= 1)
        assertTrue(calls.all { it.second["User-Agent"] == RequestQueue.USER_AGENT })
        assertTrue(calls.none { "google" in it.first || "youtube" in it.first })
        val added = e.store.candidates("fresh").first { it.title == "Brand New Zq" }
        assertEquals("2026-10-20", added.externalIds["original_date"])
        // a second refill hits the cache, not the network
        val n = calls.size
        e.refill()
        assertEquals(n, calls.size)
    }

    @Test fun requestQueue_budget_and_backoff() {
        var fail = true
        var hits = 0
        val store = InMemoryEngineStore()
        var t = 1000.0
        val q = RequestQueue(store, { _, _ -> hits++; if (fail) throw RuntimeException("HTTP 503") else "{}" }, { t }, dailyBudget = 3, sleeper = {})
        assertNull(q.get("https://musicbrainz.org/ws/2/x"))
        assertTrue(q.backoffUntil("musicbrainz.org") > t)
        assertNull(q.get("https://musicbrainz.org/ws/2/y")); assertEquals("backoff:musicbrainz.org", q.lastSkipReason)
        assertEquals(1, hits)
        t += 61; fail = false
        assertEquals("{}", q.get("https://musicbrainz.org/ws/2/z"))
        assertEquals("{}", q.get("https://ws.audioscrobbler.com/2.0/?a"))
        assertNull(q.get("https://ws.audioscrobbler.com/2.0/?b")); assertEquals("daily_budget", q.lastSkipReason)
        assertEquals(3, hits)
    }

    @Test fun refill_withLastfmKey_addsSimilarAndListeners() {
        val net = NetFetcher { url, _ ->
            when {
                "track.getsimilar" in url -> """{"similartracks":{"track":[{"name":"Sim Song Zq","match":"0.9","artist":{"name":"Sim Artist Zq"}}]}}"""
                "artist.gettoptracks" in url -> """{"toptracks":{"track":[{"name":"Top Zq","listeners":"123456"}]}}"""
                "track.getinfo" in url -> """{"track":{"listeners":"5000000"}}"""
                "artist.getinfo" in url -> """{"artist":{"stats":{"listeners":"9000000"}}}"""
                "musicbrainz" in url -> """{"artists":[],"recordings":[]}"""
                else -> error(url)
            }
        }
        val e = engine(net = net)
        e.rateShown(e.next()!!, "love")
        e.lastfmApiKey = "k123"
        val rep = e.refill()
        assertEquals(1, rep.funnel["lastfm_similar"]!!["added"])
        assertTrue(e.store.candidates("proven").any { it.title == "Sim Song Zq" })
        assertTrue(e.store.metaWithPrefix("lfm_listeners:track:").isNotEmpty())
    }

    // ---- 4. friends mode ----
    @Test fun friends_queueAndProfiles_noAutomaticTransfer() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val r = e.next()!!
        val p0 = e.profileSizes()["personal"]!!
        e.rateShown(r, "love")
        // no Last.fm data → B ≤ 0.58 → empty queue
        assertTrue(e.friendsQueue().none { it.rec.title == r.title })
        // give it listener data at the top of the reference catalog
        for (i in 1..20) { store.setMeta("lfm_listeners:track:ref$i", (i * 1000).toString()); store.setMeta("lfm_listeners:artist:ref$i", (i * 1e4).toString()) }
        store.setMeta("lfm_listeners:track:${normalizeKey(r.artist, r.title)}", "1e9")
        store.setMeta("lfm_listeners:artist:${Affinity.artistKey(r.artist)}", "1e9")
        val q = e.friendsQueue()
        val card = q.first { it.rec.title == r.title }
        assertTrue(card.broad >= BroadAppeal.MIN_B); assertEquals(1.0, card.personal, 0.0)
        assertEquals(q.sortedByDescending { it.personal * it.broad }, q)

        val ratingsBefore = store.ratings(1000).size
        val weightsBefore = store.allWeights()
        e.groupReact(card.rec, "didnt_work")
        e.friendsContextRate(card.rec, "meh")
        assertEquals(ratingsBefore, store.ratings(1000).size)
        assertEquals(weightsBefore, store.allWeights())
        assertEquals(mapOf("personal" to p0 + 1, "personal_friends_context" to 1, "audience:group" to 1), e.profileSizes())
        // explicit teach → personal
        e.teachPersonal(card.rec, "like")
        assertEquals(ratingsBefore + 1, store.ratings(1000).size)
        // undo reverses teach, then the friends-context judgment
        e.undo(); e.undo()
        assertEquals(mapOf("personal" to p0 + 1, "personal_friends_context" to 0, "audience:group" to 1), e.profileSizes())
        val aud = e.friendsAudition(5)
        assertTrue(aud.isNotEmpty() && aud.all { it.rec.lane == "audition" })
    }
}
