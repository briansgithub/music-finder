package com.brian.musicrec.core.engine

import com.brian.musicrec.core.Recommendation
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate
import java.util.zip.GZIPInputStream
import kotlin.random.Random

/** Next-card prefetch: no side effects until shown, exact undo, logging on display, invalidation → swap. */
class PrefetchTest {
    private val bundle = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream()).bufferedReader().readText()
    private var now = 1_800_000_000.0

    private fun engine(store: InMemoryEngineStore = InMemoryEngineStore()): LocalEngine {
        val e = LocalEngine(store, net = null, allowNetworkResolve = { false }, random = Random(5),
            clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) })
        e.importText(bundle)
        return e
    }

    private fun key(r: Recommendation) = normalizeKey(r.artist, r.title)

    @Test fun prefetch_hasNoSideEffects_andMatchesWhatNextWouldServe() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        e.next()!!
        val logs = store.logCount()
        val ratings = store.ratings(1000)
        val weights = store.allWeights()
        val p = e.prefetchNext()!!
        assertTrue(p.computeMs >= 0)
        assertEquals("prefetch must not log", logs, store.logCount())
        assertEquals(ratings, store.ratings(1000))
        assertEquals(weights, store.allWeights())
        assertNull(store.lastLog(p.key))
        assertNull(e.prefetchInvalidReason(p))
        assertEquals("deterministic: same card next() would serve", p.key, key(e.next()!!))
    }

    @Test fun ratingTap_showsPrefetched_logsOnDisplay_onceOnly() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val cur = e.next()!!
        val p = e.prefetchNext()!!
        val logs = store.logCount()
        val (depth, shown) = e.rateAndShowPrefetched(cur, "like", p)
        assertEquals(1, depth)
        assertTrue(shown.usedPrefetch); assertNull(shown.reason)
        assertEquals(p.key, key(shown.rec!!))
        assertEquals(logs + 1, store.logCount())
        val log = Json.parseToJsonElement(store.lastLog(p.key)!!.metricsSnapshot).jsonObject["_log"]!!.jsonObject
        assertNotNull(log["session"])
        assertEquals("like", store.ratings(1).first().rating)
        // the following card never repeats the shown one
        assertNotEquals(p.key, key(e.next()!!))
    }

    @Test fun undo_afterPrefetchedTap_restoresExactly() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val cur = e.next()!!
        val ratings = store.ratings(1000)
        val weights = store.allWeights()
        val tags = store.allTags()
        val logs = store.logCount()
        val p = e.prefetchNext()!!
        val (_, shown) = e.rateAndShowPrefetched(cur, "dislike", p)
        val res = e.undo(displacedKey = key(shown.rec!!))!!
        assertEquals(key(cur), key(res.rec))
        assertEquals(ratings, store.ratings(1000))
        assertEquals(weights, store.allWeights())
        // Rated card's tag row restored exactly. (Showing a card bumps vocabulary "seen" counts
        // for that card's artist/genre/tags — same as the non-prefetch path; undo never reverted those.)
        val disp = shown.rec!!
        val seenByDisplay = setOf(disp.artist, disp.cluster) + disp.tags
        fun relevant(m: Map<Pair<String, String>, TagRow>) = m.filterKeys { (t, _) -> seenByDisplay.none { Py.casefold(it).trim() == Py.casefold(t).trim() } }
        assertEquals(relevant(tags), relevant(store.allTags()))
        assertEquals("displaced card un-served", logs, store.logCount())
        // the displaced card is available again
        assertEquals(p.key, key(e.next()!!))
    }

    @Test fun mehOnThePrefetchedWork_invalidates_andSwapsFreshPick() {
        val store = InMemoryEngineStore()
        val e = engine(store)
        val cur = e.next()!!
        val p = e.prefetchNext()!!
        // e.g. the same work was rated meh elsewhere (friends teach / another device import)
        e.rate(p.rec.artist, p.rec.title, "meh", p.rec.lane, p.rec.cluster)
        assertNotNull(e.prefetchInvalidReason(p))
        val logs = store.logCount()
        val (_, s) = e.rateAndShowPrefetched(cur, "like", p)
        assertFalse(s.usedPrefetch)
        assertNotNull(s.reason)
        assertNotEquals(p.key, key(s.rec!!))
        assertEquals("only the replacement is logged", logs + 1, store.logCount())
        assertNull(store.lastLog(p.key))
    }

    @Test fun dislikedArtist_invalidates() {
        val e = engine()
        e.next()!!
        val p = e.prefetchNext()!!
        e.rate(p.rec.artist, "Some Other Song Zq", "dislike")
        assertTrue(e.prefetchInvalidReason(p)!!.contains("disliked") || e.prefetchInvalidReason(p)!!.contains("suppressed"))
    }

    @Test fun undo_dials_lane_chips_friends_settings_invalidate() {
        val e = engine()
        val cur = e.next()!!
        e.rate(cur.artist, cur.title, "like", cur.lane, cur.cluster, shown = cur)
        e.next()!!
        var p = e.prefetchNext()!!
        assertNull(e.prefetchInvalidReason(p))
        assertEquals("lane or chips changed", e.prefetchInvalidReason(p, lane = "proven"))
        assertEquals("lane or chips changed", e.prefetchInvalidReason(p, enabledGenres = listOf("game")))
        e.setDials("fresh", "", null)
        assertTrue(e.prefetchInvalidReason(p)!!.startsWith("state changed"))
        e.setDials("auto", "", null)
        p = e.prefetchNext()!!
        e.groupReact(p.rec, "liked")
        assertTrue(e.prefetchInvalidReason(p)!!.startsWith("state changed"))
        p = e.prefetchNext()!!
        e.undo()
        assertTrue(e.prefetchInvalidReason(p)!!.startsWith("state changed"))
        p = e.prefetchNext()!!
        e.freshMinConfidence = 0.55
        assertTrue(e.prefetchInvalidReason(p)!!.startsWith("state changed"))
        p = e.prefetchNext()!!
        e.next()!! // a different card was served meanwhile
        assertNotNull(e.prefetchInvalidReason(p))
    }

    @Test fun artistSpacing_stillHolds_withPrefetch() {
        val e = engine()
        var cur = e.next()!!
        val shown = mutableListOf(cur)
        repeat(12) {
            val p = e.prefetchNext()!!
            val (_, s) = e.rateAndShowPrefetched(cur, "like", p)
            cur = s.rec!!
            shown += cur
        }
        for (i in shown.indices) {
            val window = shown.subList(maxOf(0, i - Diversity.ARTIST_SPACING + 1), i).map { Affinity.artistKey(it.artist) }
            assertFalse("artist repeated within 5 at $i: ${shown.map { it.artist }}", Affinity.artistKey(shown[i].artist) in window)
        }
    }
}
