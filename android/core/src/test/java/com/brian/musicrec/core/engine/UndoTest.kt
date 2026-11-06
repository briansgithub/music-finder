package com.brian.musicrec.core.engine

import com.brian.musicrec.core.Recommendation
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.LocalDate
import java.util.zip.GZIPInputStream
import kotlin.random.Random

/** Undo: exact reversal of rating rows, learning, tags, session suppression and pool removal. */
class UndoTest {
    private val bundle = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream()).bufferedReader().readText()
    private var now = 1_800_000_000.0
    private lateinit var store: InMemoryEngineStore

    private fun engine(): LocalEngine {
        store = InMemoryEngineStore()
        val e = LocalEngine(store, random = Random(7), clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) })
        e.importText(bundle)
        return e
    }

    private fun LocalEngine.rateShown(r: Recommendation, rating: String) =
        rate(r.artist, r.title, rating, r.lane, r.cluster, r.confidence, shown = r)

    @Test fun nothingToUndo_initially() {
        val e = engine()
        assertEquals(0, e.undoDepth())
        assertNull(e.undo())
    }

    @Test fun undoLike_restoresRatingWeightsAndTagsExactly_andReshowsCard() {
        val e = engine()
        val card = e.next()!!
        e.learning.ensurePriors()
        val weightsBefore = store.allWeights()
        val tagsBefore = store.allTags()
        val ratingsBefore = store.ratings(100_000)
        e.rateShown(card, "like")
        assertNotEquals(weightsBefore, store.allWeights())
        assertEquals(1, e.undoDepth())
        val res = e.undo()!!
        assertEquals(card, res.rec)
        assertEquals(0, res.remaining)
        assertEquals(weightsBefore, store.allWeights())
        assertEquals(tagsBefore, store.allTags())
        assertEquals(ratingsBefore, store.ratings(100_000))
    }

    @Test fun undo_returnsDisplacedCardToPool_andKeepsUndoneCardCurrent() {
        val e = engine()
        val card = e.next()!!
        e.rateShown(card, "like")
        val next = e.next()!!
        val nextKey = normalizeKey(next.artist, next.title)
        assertFalse(nextKey in e.readyPoolKeys())
        val res = e.undo(nextKey)!!
        assertEquals(card, res.rec)
        val pool = e.readyPoolKeys()
        assertTrue(nextKey in pool)
        assertFalse(normalizeKey(card.artist, card.title) in pool)
    }

    @Test fun undoSkip_reversesSessionSuppression() {
        val e = engine()
        val base = store.candidates("proven").first()
        val card = Recommendation(artist = base.artist, title = base.title + " (Live)", lane = "proven", confidence = 0.6)
        val weightsBefore = store.allWeights()
        e.rateShown(card, "skip")
        assertTrue(e.session.blockedPatterns[Py.casefold(base.artist)]!!.contains("live"))
        val res = e.undo()!!
        assertEquals(card, res.rec)
        assertNull(e.session.blockedPatterns[Py.casefold(base.artist)])
        assertEquals(weightsBefore, store.allWeights())
    }

    @Test fun undoNotNow_removesFeedback_noWeightChange() {
        val e = engine()
        val card = e.next()!!
        val fb = store.feedback.size
        val w = store.allWeights()
        e.notNow(card.artist, card.title, card.lane, shown = card)
        assertEquals(fb + 1, store.feedback.size)
        assertEquals(1, e.undoDepth())
        assertEquals(card, e.undo()!!.rec)
        assertEquals(fb, store.feedback.size)
        assertEquals(w, store.allWeights())
    }

    @Test fun multiLevelUndo_reversesInOrder_backToOriginalState() {
        val e = engine()
        e.learning.ensurePriors()
        val w0 = store.allWeights()
        val r0 = store.ratings(100_000).size
        val shown = ArrayList<Recommendation>()
        val ratings = listOf("love", "meh", "skip", "like")
        for (rt in ratings) {
            val c = e.next()!!
            shown += c
            if (rt == "meh") e.notNow(c.artist, c.title, c.lane, shown = c) else e.rateShown(c, rt)
        }
        var current = e.next()
        assertEquals(4, e.undoDepth())
        for (i in shown.indices.reversed()) {
            val res = e.undo(current?.let { normalizeKey(it.artist, it.title) })!!
            assertEquals(shown[i], res.rec)
            assertEquals(i, res.remaining)
            current = res.rec
        }
        assertNull(e.undo())
        assertEquals(w0, store.allWeights())
        assertEquals(r0, store.ratings(100_000).size)
    }

    @Test fun undoStack_isCapped() {
        val e = engine()
        repeat(LocalEngine.MAX_UNDO + 3) {
            val c = e.next()!!
            e.rateShown(c, "skip")
        }
        assertEquals(LocalEngine.MAX_UNDO, e.undoDepth())
    }
}
