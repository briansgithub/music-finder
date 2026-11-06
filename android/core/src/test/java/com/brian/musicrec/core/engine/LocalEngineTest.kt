package com.brian.musicrec.core.engine

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
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

/** End-to-end on-device engine behaviour (offline): /next, /rate, /not_now, /dials, /export, resolve. */
class LocalEngineTest {
    private val bundle = GZIPInputStream(File("src/main/assets/seed/musicfinder_seed.json.gz").inputStream()).bufferedReader().readText()
    private var now = 1_800_000_000.0

    private fun engine(net: NetFetcher? = null, online: Boolean = false): LocalEngine {
        val e = LocalEngine(
            InMemoryEngineStore(), net = net, allowNetworkResolve = { online }, random = Random(42),
            clock = { now += 1.0; now }, today = { LocalDate.of(2026, 11, 6) },
        )
        assertFalse(e.hasSeed())
        e.importText(bundle)
        assertTrue(e.hasSeed())
        return e
    }

    @Test fun nextRateSkip_offline() {
        val e = engine()
        val r = e.next()
        assertNotNull(r); r!!
        assertTrue(r.confidence in 0.0..1.0)
        assertTrue(r.ytmUrl.startsWith("https://music.youtube.com/"))
        assertEquals("handoff_youtube_music", r.playback)
        e.rate(r.artist, r.title, "skip", r.lane, r.cluster, r.confidence)
        val rated = e.store.ratings(1).first()
        assertEquals("skip", rated.rating)
        assertEquals(r.confidence, rated.predictedConfidence!!, 0.0)
        // skipped + served keys never come back
        val seen = HashSet<String>().apply { add(normalizeKey(r.artist, r.title)) }
        repeat(25) {
            val n = e.next() ?: return@repeat
            assertTrue("repeat ${n.artist} – ${n.title}", seen.add(normalizeKey(n.artist, n.title)))
            e.rate(n.artist, n.title, if (it % 2 == 0) "like" else "skip", n.lane, n.cluster, n.confidence)
        }
    }

    @Test fun rateWithoutConfidence_usesLastShown() {
        val e = engine()
        val r = e.next()!!
        e.rate(r.artist, r.title, "love", r.lane)
        assertEquals(r.confidence, e.store.ratings(1).first().predictedConfidence!!, 0.0)
    }

    @Test fun notNow_hasNoTastePenalty() {
        val e = engine()
        val r = e.next()!!
        val before = e.store.getWeight("global", "deezer")
        val nRatings = e.store.ratings().size
        e.notNow(r.artist, r.title, r.lane)
        assertEquals(before, e.store.getWeight("global", "deezer"))
        assertEquals(nRatings, e.store.ratings().size)
    }

    @Test fun genreChips_orInclude_nullUnconstrained_emptyNothing() {
        val e = engine()
        val d = e.dials()
        assertNull(d.enabledGenres)
        assertTrue(d.availableGenres.containsAll(listOf("classical", "electronic", "game")))
        assertNull(e.next(enabledGenres = emptyList()))
        val allowed = setOf("electronic", "game", "soundtrack")
        e.setDials("auto", "", allowed.toList())
        var got = 0
        while (true) {
            val r = e.next() ?: break
            val tags = (r.tags + r.cluster).map { Py.casefold(it) }.toSet()
            assertTrue("${r.artist} – ${r.title} $tags", tags.any { it in allowed })
            got++
            if (got > 50) break
        }
        assertTrue(got > 0)
        assertEquals(allowed.sorted(), e.dials().enabledGenres)
        e.setDials("auto", "", null)
        assertNull(e.dials().enabledGenres)
    }

    @Test fun export_thenImportExport_roundTrip() {
        val e = engine()
        val r = e.next()!!
        e.rate(r.artist, r.title, "like", r.lane, r.cluster, r.confidence)
        val exported = Json.parseToJsonElement(e.exportJson()).jsonObject
        assertEquals("musicfinder-export", exported["format"]!!.jsonPrimitive.content)
        assertEquals(e.store.ratings().size, exported["ratings"]!!.jsonArray.size)
        assertEquals("like", exported["ratings"]!!.jsonArray[0].jsonObject["rating"]!!.jsonPrimitive.content)

        val e2 = engine()
        val s = e2.importText(e.exportJson())
        assertEquals("export", s.kind)
        assertEquals(1, s.ratingsAdded)
        assertEquals(0, e2.importText(e.exportJson()).ratingsAdded) // idempotent
    }

    @Test fun pcExportJson_isAccepted() {
        val pc = """{"exported_at": 1.0, "ratings": [{"artist":"LukHash","title":"Alpha","key":"lukhash::alpha",
            "rating":"love","lane":"proven","cluster":"electronic","predicted_confidence":0.71,"created_at":1793900000.5}],
            "dials": {"enabled_genres": ["electronic"]}, "tags": [{"tag":"synthwave","kind":"tag","count":7,"as_dial":1}]}"""
        val e = engine()
        val s = e.importText(pc)
        assertEquals(1, s.ratingsAdded)
        assertEquals(listOf("electronic"), e.dials().enabledGenres)
    }

    @Test fun videoResolve_localFirst_correctionLast_networkOnlyWhenNeeded() {
        var calls = 0
        val net = NetFetcher { url, _ ->
            calls++
            if (url.contains("/recording?")) """{"recordings":[{"id":"abc","title":"Unknown Tune"}]}"""
            else """{"relations":[{"url":{"resource":"https://www.youtube.com/watch?v=AAAAAAAAAAA"}}]}"""
        }
        val e = engine(net, online = true)
        val store = e.store
        store.putSeed(SeedRow("Seed Artist", "Seed Song", normalizeKey("Seed Artist", "Seed Song"), "yt_liked", "BBBBBBBBBBB"))
        val local = e.resolver.resolve("Seed Artist", "Seed Song")
        assertEquals("BBBBBBBBBBB", local.videoId)
        assertEquals(0, calls)
        val mb = e.resolver.resolve("Some Artist", "Unknown Tune")
        assertEquals("AAAAAAAAAAA", mb.videoId)
        assertEquals(2, calls)
        e.resolver.resolve("Some Artist", "Unknown Tune") // cached now
        assertEquals(2, calls)
        e.correctVideo("Some Artist", "Unknown Tune", "https://music.youtube.com/watch?v=CCCCCCCCCCC")
        val fixed = e.resolver.resolve("Some Artist", "Unknown Tune")
        assertEquals("CCCCCCCCCCC", fixed.videoId)
        assertTrue(fixed.override)
        // offline: no network, search fallback
        val off = engine(net, online = false)
        val r = off.resolver.resolve("Nobody", "Nothing")
        assertFalse(r.ok)
        assertEquals(2, calls)
    }
}
