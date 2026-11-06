package com.brian.musicrec.ui

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.compose.viewModel
import com.brian.musicrec.core.DebugLog
import com.brian.musicrec.core.DialState
import com.brian.musicrec.core.LocalEngineHolder
import com.brian.musicrec.core.MusicEngine
import com.brian.musicrec.core.OnDeviceMusicEngine
import com.brian.musicrec.core.Recommendation
import com.brian.musicrec.core.RemoteMusicEngine
import com.brian.musicrec.core.YtmHandoff
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

private val DEFAULT_CHIPS = listOf(
    "classical", "ragtime", "soundtrack", "game", "electronic",
    "metal", "pop", "jazz", "piano", "ambient", "folk", "vocal",
    "trance", "instrumental",
)

class RecViewModel(app: Application) : AndroidViewModel(app) {
    private val prefs = app.getSharedPreferences("musicfinder", Context.MODE_PRIVATE)

    /** Default: on-device engine. Remote PC engine is optional. */
    var useRemote by mutableStateOf(prefs.getBoolean("use_remote", false))
        private set
    var baseUrl by mutableStateOf(prefs.getString("remote_url", "http://127.0.0.1:8765") ?: "http://127.0.0.1:8765")
    var allowOnlineLookup by mutableStateOf(prefs.getBoolean("online_video_lookup", true))
        private set
    var rec by mutableStateOf<Recommendation?>(null)
    var dials by mutableStateOf(DialState())
    var status by mutableStateOf("Ready")
    var whyOpen by mutableStateOf(false)
    var busy by mutableStateOf(false)
    var engineReachable by mutableStateOf(true)
    var engineInfo by mutableStateOf("")
    /** How many ratings/not-nows can be undone (Undo hidden at 0). */
    var undoDepth by mutableStateOf(0)
    /** Fresh lane ran dry (on-device): show the labeled 90-day expansion offer. */
    var freshExhausted by mutableStateOf<String?>(null)
    var freshExpand90 by mutableStateOf(prefs.getBoolean("fresh_expand_90", false))
        private set
    /** Optional Last.fm API key (Settings). Blank = Last.fm sources skipped. Never a Google account. */
    var lastfmKey by mutableStateOf(prefs.getString("lastfm_api_key", "") ?: "")
    var funnelInfo by mutableStateOf("")
    var friendsQueue by mutableStateOf<List<com.brian.musicrec.core.engine.LocalEngine.FriendsCard>>(emptyList())
    var friendsAudition by mutableStateOf<List<com.brian.musicrec.core.engine.LocalEngine.FriendsCard>>(emptyList())
    var friendsStatus by mutableStateOf("")

    init {
        LocalEngineHolder.allowNetworkResolve = allowOnlineLookup
        val saved = prefs.getString("enabled_genres", null)
        if (saved != null) dials = dials.copy(enabledGenres = if (saved.isEmpty()) emptyList() else saved.split('\u0001'))
        dials = dials.copy(lane = prefs.getString("lane", "auto") ?: "auto")
    }

    private fun engine(): MusicEngine =
        if (useRemote) RemoteMusicEngine(baseUrl)
        else OnDeviceMusicEngine(LocalEngineHolder.get(getApplication()).also {
            it.freshMinConfidence = freshMinConfidence.toDouble()
            it.freshExpandDays = if (freshExpand90) 90 else null
            it.lastfmApiKey = lastfmKey
        })

    private fun persistDials() {
        prefs.edit()
            .putString("lane", dials.lane)
            .apply { val e = dials.enabledGenres; if (e == null) remove("enabled_genres") else putString("enabled_genres", e.joinToString("\u0001")) }
            .apply()
    }

    fun setRemote(on: Boolean) {
        useRemote = on
        prefs.edit().putBoolean("use_remote", on).putString("remote_url", baseUrl).apply()
        DebugLog.info("engine mode -> ${if (on) "remote $baseUrl" else "on-device"}")
        rec = null
        undoDepth = if (on) 0 else LocalEngineHolder.get(getApplication()).undoDepth()
        refreshDials()
        loadNext()
    }

    fun saveUrl() = prefs.edit().putString("remote_url", baseUrl).apply()

    fun setOnlineLookup(on: Boolean) {
        allowOnlineLookup = on
        LocalEngineHolder.allowNetworkResolve = on
        prefs.edit().putBoolean("online_video_lookup", on).apply()
    }

    fun loadNext() = viewModelScope.launch {
        busy = true
        status = "Loading…"
        try {
            val r = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] next(lane=${dials.lane}, genres=${dials.enabledGenres ?: "all"})") {
                    e.next(dials.lane.ifBlank { "auto" }, dials.enabledGenres)
                }
            }
            rec = r
            engineReachable = true
            if (r != null && r.ytmHandoff != "watch" && !useRemote) upgradeVideoLater(r)
            val ex = if (r == null && !useRemote) withContext(Dispatchers.IO) { LocalEngineHolder.get(getApplication()).lastExhaustion } else null
            freshExhausted = ex?.let {
                "No fresh releases left in the ${it["fresh_days"]}-day window (after filters)." +
                    if (it["can_expand"] == true) " You can widen it to 90 days — those cards are labeled “fresh (90-day)”." else ""
            }
            status = when {
                freshExhausted != null -> "Fresh lane exhausted"
                r == null -> "No recommendation — enable more genres, or import a fresher seed in Settings."
                else -> "OK · ${if (useRemote) "remote" else "on-device"}"
            }
        } catch (e: Exception) {
            engineReachable = false
            status = "Engine error: ${e.message}"
        } finally {
            busy = false
        }
    }

    /** Skip / Love / Like / Meh: record rating (with shown confidence), then load the next card. */
    fun rate(rating: String) = viewModelScope.launch {
        val r = rec ?: return@launch
        busy = true
        try {
            val depth = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] rate($rating, ${r.artist} – ${r.title})") { e.rate(r, rating) }
            }
            undoDepth = maxOf(0, depth)
            loadNext()
        } catch (e: Exception) {
            status = "rate failed: ${e.message}"
            busy = false
        }
    }

    fun skip() = rate("skip")

    /** A background video lookup may finish after the card is shown: swap in the exact video if it's still current. */
    private fun upgradeVideoLater(r: Recommendation) = viewModelScope.launch {
        val up = withContext(Dispatchers.IO) { runCatching { engine().awaitVideo(r) }.getOrNull() } ?: return@launch
        if (rec?.artist == r.artist && rec?.title == r.title) {
            rec = up
            DebugLog.info("video found in background: ${up.artist} – ${up.title} → ${up.videoId} (${up.resolveSource})")
        }
    }

    var freshMinConfidence by mutableStateOf(prefs.getFloat("fresh_min_confidence", 0.70f))

    fun setFreshMin(v: Float) {
        freshMinConfidence = (Math.round(v * 20f) / 20f).coerceIn(0f, 0.95f)
        prefs.edit().putFloat("fresh_min_confidence", freshMinConfidence).apply()
        if (!useRemote) viewModelScope.launch(Dispatchers.IO) {
            LocalEngineHolder.get(getApplication()).freshMinConfidence = freshMinConfidence.toDouble()
        }
    }

    fun setExpand90(on: Boolean) {
        freshExpand90 = on
        prefs.edit().putBoolean("fresh_expand_90", on).apply()
        if (!useRemote) viewModelScope.launch(Dispatchers.IO) {
            LocalEngineHolder.get(getApplication()).freshExpandDays = if (on) 90 else null
        }
    }

    fun expandFreshAndRetry() { setExpand90(true); freshExhausted = null; loadNext() }

    fun saveLastfmKey(v: String) {
        lastfmKey = v.trim()
        prefs.edit().putString("lastfm_api_key", lastfmKey).apply()
        if (!useRemote) viewModelScope.launch(Dispatchers.IO) { LocalEngineHolder.get(getApplication()).lastfmApiKey = lastfmKey }
    }

    /** On-device refill: Last.fm (if a key is set) + MusicBrainz, through the one rate-limited queue. */
    fun refill() = viewModelScope.launch {
        if (useRemote) { status = "Refill runs on-device only"; return@launch }
        busy = true
        status = "Refilling candidates (rate-limited)…"
        try {
            val rep = withContext(Dispatchers.IO) {
                val e = LocalEngineHolder.get(getApplication()).also { it.lastfmApiKey = lastfmKey }
                DebugLog.call("refill") { e.refill() }
            }
            status = "Refill: +${rep.added()} candidates · ${rep.notes.joinToString(" · ")}"
            DebugLog.info("refill funnel: ${rep.funnel}")
            refreshFunnel()
        } catch (e: Exception) {
            status = "refill failed: ${e.message}"
        } finally {
            busy = false
        }
    }

    fun refreshFunnel() = viewModelScope.launch {
        funnelInfo = withContext(Dispatchers.IO) {
            runCatching {
                LocalEngineHolder.get(getApplication()).funnelReport().entries.joinToString("\n") { "${it.key}: ${it.value}" }
            }.getOrElse { "funnel unavailable: ${it.message}" }
        }
    }

    // ---- Friends mode (on-device) ----
    fun loadFriends() = viewModelScope.launch {
        if (useRemote) { friendsStatus = "Friends mode runs on-device only"; return@launch }
        val (q, a) = withContext(Dispatchers.IO) {
            val e = LocalEngineHolder.get(getApplication())
            e.friendsQueue() to e.friendsAudition()
        }
        friendsQueue = q
        friendsAudition = a
        friendsStatus = if (q.isEmpty()) "No Loved/Liked tracks reach broad appeal ≥ 0.70 yet. Broad appeal needs Last.fm listener counts: add a Last.fm API key in Settings and run Refill." else "${q.size} crowd-pleasers"
    }

    fun groupReact(card: com.brian.musicrec.core.engine.LocalEngine.FriendsCard, reaction: String) = viewModelScope.launch {
        withContext(Dispatchers.IO) { LocalEngineHolder.get(getApplication()).groupReact(card.rec, reaction) }
        undoDepth = LocalEngineHolder.get(getApplication()).undoDepth()
        friendsStatus = "Group reaction saved (${reaction.replace('_', ' ')}) — group profile only"
    }

    fun friendsContextRate(card: com.brian.musicrec.core.engine.LocalEngine.FriendsCard, rating: String) = viewModelScope.launch {
        withContext(Dispatchers.IO) { LocalEngineHolder.get(getApplication()).friendsContextRate(card.rec, rating) }
        undoDepth = LocalEngineHolder.get(getApplication()).undoDepth()
        friendsStatus = "Saved $rating for friends context — your personal recs are unchanged"
    }

    fun teachPersonal(card: com.brian.musicrec.core.engine.LocalEngine.FriendsCard, rating: String) = viewModelScope.launch {
        withContext(Dispatchers.IO) { LocalEngineHolder.get(getApplication()).teachPersonal(card.rec, rating) }
        undoDepth = LocalEngineHolder.get(getApplication()).undoDepth()
        friendsStatus = "Also taught your personal recs: $rating"
    }

    fun notNow() = viewModelScope.launch {
        val r = rec ?: return@launch
        busy = true
        try {
            val depth = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] not_now(${r.artist} – ${r.title})") { e.notNow(r) }
            }
            undoDepth = maxOf(0, depth)
            // not_now never changes the lane: go back to auto.
            if (dials.lane != "auto") {
                DebugLog.info("not_now: lane ${dials.lane} -> auto")
                dials = dials.copy(lane = "auto")
                persistDials()
            }
            loadNext()
        } catch (e: Exception) {
            status = "not_now failed: ${e.message}"
            busy = false
        }
    }

    /** Revert the latest rating / not-now and re-show that card. */
    fun undo() = viewModelScope.launch {
        if (undoDepth <= 0) return@launch
        busy = true
        try {
            val shown = rec
            val res = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] undo(current=${shown?.let { "${it.artist} – ${it.title}" } ?: "-"})") { e.undo(shown) }
            }
            if (res == null) {
                undoDepth = 0
                status = "Nothing to undo"
            } else {
                rec = res.first
                undoDepth = maxOf(0, res.second)
                engineReachable = true
                status = "Undone — ${res.first.artist} – ${res.first.title} is back"
            }
        } catch (e: Exception) {
            status = "undo failed: ${e.message}"
        } finally {
            busy = false
        }
    }

    fun correctVideo(input: String) = viewModelScope.launch {
        val r = rec ?: return@launch
        try {
            val vid = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] correct_video(${r.artist} – ${r.title}, $input)") { e.correctVideo(r, input) }
            }
            rec = r.copy(videoId = vid, ytmUrl = "https://music.youtube.com/watch?v=$vid", ytmHandoff = "watch", resolveSource = "user_correction")
            status = "Saved video fix → $vid"
        } catch (e: Exception) {
            status = "video fix failed: ${e.message}"
        }
    }

    fun refreshDials() = viewModelScope.launch {
        try {
            val remote = withContext(Dispatchers.IO) {
                val e = engine()
                DebugLog.call("[${e.label}] dials()") { e.dials() }
            }
            val mergedEnabled = mergeEnabledGenres(dials.enabledGenres, dials.availableGenres, remote.availableGenres, remote.enabledGenres)
            dials = remote.copy(enabledGenres = mergedEnabled, lane = dials.lane.ifBlank { remote.lane })
            withContext(Dispatchers.IO) { runCatching { engine().setDials(dials.lane, dials.preset, dials.enabledGenres) } }
        } catch (_: Exception) {
            if (dials.availableGenres.isEmpty()) dials = dials.copy(availableGenres = DEFAULT_CHIPS)
        }
    }

    fun applyLane(lane: String) = viewModelScope.launch {
        dials = dials.copy(lane = lane)
        persistDials()
        try {
            withContext(Dispatchers.IO) { engine().setDials(lane, dials.preset, dials.enabledGenres) }
        } catch (e: Exception) {
            status = "dials: ${e.message}"
        }
    }

    fun toggleGenre(genre: String) = viewModelScope.launch {
        val available = dials.availableGenres
        val on = (dials.enabledGenres ?: available).toMutableSet()
        if (!on.add(genre)) on.remove(genre)
        val next = if (available.isNotEmpty() && on.containsAll(available)) null else on.toList()
        dials = dials.copy(enabledGenres = next)
        persistDials()
        try {
            withContext(Dispatchers.IO) { engine().setDials(dials.lane, dials.preset, next) }
            loadNext()
        } catch (e: Exception) {
            status = "genres: ${e.message}"
        }
    }

    fun importUri(ctx: Context, uri: Uri) = viewModelScope.launch {
        busy = true
        try {
            val summary = withContext(Dispatchers.IO) {
                val raw = ctx.contentResolver.openInputStream(uri)!!.use { it.readBytes() }
                val text = if (raw.size > 2 && raw[0] == 0x1f.toByte() && raw[1] == 0x8b.toByte()) {
                    java.util.zip.GZIPInputStream(raw.inputStream()).bufferedReader().readText()
                } else String(raw, Charsets.UTF_8)
                DebugLog.call("import file") { LocalEngineHolder.get(getApplication()).importText(text) }
            }
            status = "Imported: $summary"
            refreshDials()
            loadNext()
        } catch (e: Exception) {
            DebugLog.error("import failed", e)
            status = "Import failed: ${e.message}"
        } finally {
            busy = false
        }
    }

    fun reimportBundled() = viewModelScope.launch {
        val ok = withContext(Dispatchers.IO) {
            val app = getApplication<Application>()
            LocalEngineHolder.firstRun(app, LocalEngineHolder.get(app))
        }
        status = if (ok) "Bundled seed re-imported (ratings kept)" else "No bundled seed in this build"
        loadNext()
    }

    fun recalibrate() = viewModelScope.launch {
        val c = withContext(Dispatchers.IO) {
            DebugLog.call("recalibrate") { LocalEngineHolder.get(getApplication()).recalibrate() }
        }
        status = "Calibration: ${c.kind} (n=${c.n})"
    }

    fun refreshEngineInfo() = viewModelScope.launch {
        engineInfo = withContext(Dispatchers.IO) {
            try {
                LocalEngineHolder.get(getApplication()).stats().entries.joinToString("\n") { "${it.key}: ${it.value}" }
            } catch (e: Exception) {
                "on-device engine unavailable: ${e.message}"
            }
        }
    }

    fun shareText(ctx: Context, fileName: String, subject: String, text: String) {
        try {
            val dir = File(ctx.cacheDir, "shared").apply { mkdirs() }
            val f = File(dir, fileName).apply { writeText(text) }
            val uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", f)
            val send = Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_SUBJECT, subject)
                putExtra(Intent.EXTRA_STREAM, uri)
                putExtra(Intent.EXTRA_TEXT, text.takeLast(20_000))
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            ctx.startActivity(Intent.createChooser(send, subject).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            DebugLog.error("share failed", e)
            status = "share failed: ${e.message}"
        }
    }

    fun exportData(ctx: Context) = viewModelScope.launch {
        try {
            val json = withContext(Dispatchers.IO) { DebugLog.call("export") { engine().exportJson() } }
            shareText(ctx, "musicfinder-export.json", "Music Finder export", json)
        } catch (e: Exception) {
            status = "export failed: ${e.message}"
        }
    }
}

/** New chips appear ON. Local constraint is kept; all-on collapses to unconstrained (null). */
internal fun mergeEnabledGenres(
    localEnabled: List<String>?,
    prevAvailable: List<String>,
    remoteAvailable: List<String>,
    remoteEnabled: List<String>?,
): List<String>? {
    if (localEnabled == null && prevAvailable.isEmpty()) return remoteEnabled
    if (localEnabled == null) return null
    val prevAvail = prevAvailable.toSet()
    val newlyOn = if (prevAvail.isEmpty()) emptyList() else remoteAvailable.filter { it !in prevAvail }
    val kept = (localEnabled + newlyOn).distinct()
    return if (remoteAvailable.isNotEmpty() && kept.containsAll(remoteAvailable)) null else kept
}

private enum class Screen { MAIN, SETTINGS, DEBUG, FRIENDS }

@Composable
fun MusicrecApp(vm: RecViewModel = viewModel()) {
    var screen by remember { mutableStateOf(Screen.MAIN) }
    LaunchedEffect(Unit) {
        vm.refreshDials()
        if (vm.rec == null) vm.loadNext()
    }
    BackHandler(enabled = screen != Screen.MAIN) { screen = Screen.MAIN }
    MaterialTheme(colorScheme = darkColorScheme()) {
        when (screen) {
            Screen.MAIN -> MainScreen(vm, onSettings = { screen = Screen.SETTINGS }, onDebug = { screen = Screen.DEBUG }, onFriends = { screen = Screen.FRIENDS })
            Screen.FRIENDS -> FriendsScreen(vm, onBack = { screen = Screen.MAIN })
            Screen.SETTINGS -> SettingsScreen(vm, onBack = { screen = Screen.MAIN })
            Screen.DEBUG -> DebugScreen(vm, onBack = { screen = Screen.MAIN })
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun MainScreen(vm: RecViewModel, onSettings: () -> Unit, onDebug: () -> Unit, onFriends: () -> Unit) {
    val ctx = LocalContext.current
    val rec = vm.rec
    var fixOpen by remember { mutableStateOf(false) }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Music Finder") },
                actions = {
                    TextButton(onClick = onFriends, enabled = !vm.useRemote) { Text("Friends") }
                    TextButton(onClick = onDebug) { Text("Debug") }
                    TextButton(onClick = onSettings) { Text("Settings") }
                },
            )
        },
    ) { pad ->
        Column(
            Modifier.padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(
                "Engine: ${if (vm.useRemote) "remote (${vm.baseUrl})" else "on-device (offline)"} · Play opens YouTube Music.",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(vm.status, style = MaterialTheme.typography.bodySmall)
            Card(Modifier.fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    when {
                        vm.busy && rec == null -> Text("Loading a suggestion…")
                        rec == null -> Text("No recommendation yet. Enable more genres, or check Settings / Debug.")
                        else -> {
                            Text(rec.title, style = MaterialTheme.typography.headlineSmall)
                            Text(rec.artist, color = MaterialTheme.colorScheme.primary)
                            // Match label (not a %) until there are ≥ 50 ratings; then p(like).
                            val label = rec.matchLabel.ifEmpty { "conf ${"%.2f".format(rec.confidence)}" }
                            Text("[${rec.laneLabel.ifEmpty { rec.lane }}] $label · ${if (rec.ytmHandoff == "watch") "exact video" else "YTM search"}")
                            if (rec.tags.isNotEmpty()) Text(rec.tags.take(8).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
                            if (vm.whyOpen) {
                                Text(rec.why, style = MaterialTheme.typography.bodyMedium)
                                Text(
                                    "affinity ${"%.3f".format(rec.affinity)} · selection ${"%.3f".format(rec.selection)} · p(like) ${"%.2f".format(rec.confidence)}" +
                                        if (rec.slot.isNotEmpty()) " · ${rec.slot}" else "",
                                    style = MaterialTheme.typography.bodySmall,
                                )
                            }
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { vm.skip() }, enabled = rec != null && !vm.busy) { Text("Skip") }
                        Button(onClick = { rec?.let { YtmHandoff.open(ctx, it) } }, enabled = rec != null) { Text("Play") }
                        TextButton(onClick = { vm.whyOpen = !vm.whyOpen }) { Text(if (vm.whyOpen) "Hide why" else "Why this") }
                    }
                    if (vm.undoDepth > 0) {
                        TextButton(onClick = { vm.undo() }, enabled = !vm.busy) {
                            Text(if (vm.undoDepth > 1) "Undo last rating (${vm.undoDepth})" else "Undo last rating")
                        }
                    }
                    vm.freshExhausted?.let { msg ->
                        Text(msg, style = MaterialTheme.typography.bodyMedium)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            if (!vm.freshExpand90) Button(onClick = { vm.expandFreshAndRetry() }, enabled = !vm.busy) { Text("Expand to 90 days") }
                            TextButton(onClick = { vm.freshExhausted = null; vm.applyLane("auto"); vm.loadNext() }) { Text("Back to auto") }
                        }
                    }
                    if (!vm.engineReachable || rec == null) {
                        TextButton(onClick = { vm.loadNext() }, enabled = !vm.busy) { Text("Retry") }
                    }
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        val canRate = rec != null && !vm.busy
                        TextButton(onClick = { vm.rate("love") }, enabled = canRate) { Text("Love") }
                        TextButton(onClick = { vm.rate("like") }, enabled = canRate) { Text("Like") }
                        TextButton(onClick = { vm.rate("meh") }, enabled = canRate) { Text("Meh") }
                        TextButton(onClick = { vm.rate("dislike") }, enabled = canRate) { Text("Dislike") }
                        TextButton(onClick = { vm.notNow() }, enabled = canRate) { Text("Not now") }
                        TextButton(onClick = { fixOpen = true }, enabled = rec != null) { Text("Wrong song?") }
                    }
                }
            }

            Text("Genres", style = MaterialTheme.typography.titleMedium)
            Text(
                "All on by default (unconstrained). Turn chips off to narrow — suggestions match any chip that is still on.",
                style = MaterialTheme.typography.bodySmall,
            )
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                vm.dials.availableGenres.forEach { genre ->
                    FilterChip(selected = vm.dials.chipOn(genre), onClick = { vm.toggleGenre(genre) }, label = { Text(genre) })
                }
            }
            Text("Lane", style = MaterialTheme.typography.titleMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                listOf("auto", "proven", "fresh").forEach { lane ->
                    FilterChip(selected = vm.dials.lane == lane, onClick = { vm.applyLane(lane) }, label = { Text(lane) })
                }
            }
            if (vm.dials.availableTagDials.isNotEmpty()) {
                Text("Frequent tags/artists:", style = MaterialTheme.typography.bodySmall)
                Text(vm.dials.availableTagDials.take(24).joinToString(" · "), style = MaterialTheme.typography.bodySmall)
            }
        }
    }
    if (fixOpen && rec != null) {
        var input by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { fixOpen = false },
            title = { Text("Wrong song?") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Paste the correct YouTube / YouTube Music link (or 11-char video ID) for “${rec.title}”. It overrides automatic matching for this track.")
                    OutlinedTextField(value = input, onValueChange = { input = it }, singleLine = true, label = { Text("Link or video ID") })
                }
            },
            confirmButton = { TextButton(onClick = { vm.correctVideo(input); fixOpen = false }, enabled = input.isNotBlank()) { Text("Save") } },
            dismissButton = { TextButton(onClick = { fixOpen = false }) { Text("Cancel") } },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SettingsScreen(vm: RecViewModel, onBack: () -> Unit) {
    val ctx = LocalContext.current
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) vm.importUri(ctx, uri)
    }
    LaunchedEffect(Unit) { vm.refreshEngineInfo() }
    Scaffold(topBar = { TopAppBar(title = { Text("Settings") }, navigationIcon = { TextButton(onClick = onBack) { Text("Back") } }) }) { pad ->
        Column(
            Modifier.padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("Engine", style = MaterialTheme.typography.titleMedium)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Use remote engine (PC)")
                    Text("Off = fully on-device, works offline (default).", style = MaterialTheme.typography.bodySmall)
                }
                Switch(checked = vm.useRemote, onCheckedChange = { vm.setRemote(it) })
            }
            OutlinedTextField(
                value = vm.baseUrl,
                onValueChange = { vm.baseUrl = it; vm.saveUrl() },
                label = { Text("Remote engine URL") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                enabled = vm.useRemote,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Online video-ID lookup")
                    Text(
                        "Only when no saved/official ID exists: one rate-limited MusicBrainz query. Off = offline only (YTM search fallback).",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                Switch(checked = vm.allowOnlineLookup, onCheckedChange = { vm.setOnlineLookup(it) })
            }

            Text("Fresh lane", style = MaterialTheme.typography.titleMedium)
            Text(
                "Minimum confidence for new releases: ${"%.2f".format(vm.freshMinConfidence)}. Below this a fresh track is not offered (auto serves proven; Fresh mode reports when it runs out).",
                style = MaterialTheme.typography.bodySmall,
            )
            Slider(
                value = vm.freshMinConfidence,
                onValueChange = { vm.setFreshMin(it) },
                valueRange = 0.5f..0.9f,
                steps = 7,
                enabled = !vm.useRemote,
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text("Widen fresh window to 90 days")
                    Text("Only when you want more: cards from the wider window are labeled “fresh (90-day)”.", style = MaterialTheme.typography.bodySmall)
                }
                Switch(checked = vm.freshExpand90, onCheckedChange = { vm.setExpand90(it) }, enabled = !vm.useRemote)
            }

            Text("Discovery sources", style = MaterialTheme.typography.titleMedium)
            Text(
                "Refill pulls new candidates on the phone: Last.fm similar tracks / artist top tracks (needs your own free Last.fm API key) and " +
                    "MusicBrainz new recordings by artists you like. One polite rate-limited queue with a daily budget and back-off. " +
                    "No Google account is used for discovery.",
                style = MaterialTheme.typography.bodySmall,
            )
            var keyInput by remember { mutableStateOf(vm.lastfmKey) }
            OutlinedTextField(
                value = keyInput,
                onValueChange = { keyInput = it },
                label = { Text("Last.fm API key (optional)") },
                modifier = Modifier.fillMaxWidth(),
                singleLine = true,
                enabled = !vm.useRemote,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { vm.saveLastfmKey(keyInput) }, enabled = !vm.useRemote && keyInput.trim() != vm.lastfmKey) { Text("Save key") }
                Button(onClick = { vm.refill() }, enabled = !vm.useRemote && !vm.busy) { Text("Refill candidates now") }
            }
            if (vm.lastfmKey.isBlank()) Text("No Last.fm key: Last.fm sources are skipped (MusicBrainz still runs).", style = MaterialTheme.typography.bodySmall)

            Text("Data", style = MaterialTheme.typography.titleMedium)
            Text(
                "Import a seed bundle (musicfinder_seed.json[.gz] from the PC export script) or the PC engine's /export JSON. Ratings are merged; nothing is deleted.",
                style = MaterialTheme.typography.bodySmall,
            )
            Button(onClick = { picker.launch(arrayOf("*/*")) }, enabled = !vm.busy) { Text("Import seed / export file…") }
            Button(onClick = { vm.reimportBundled() }, enabled = !vm.busy) { Text("Re-import bundled seed pool") }
            Button(onClick = { vm.exportData(ctx) }) { Text("Export my data (share)") }
            Button(onClick = { vm.recalibrate() }, enabled = !vm.useRemote) { Text("Recalibrate confidence") }
            Text(vm.status, style = MaterialTheme.typography.bodySmall)
            Text("On-device engine", style = MaterialTheme.typography.titleMedium)
            SelectionContainer { Text(vm.engineInfo, style = MaterialTheme.typography.bodySmall, fontFamily = FontFamily.Monospace) }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DebugScreen(vm: RecViewModel, onBack: () -> Unit) {
    val ctx = LocalContext.current
    var tick by remember { mutableIntStateOf(0) }
    var errorsOnly by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        DebugLog.listener = { tick++ }
        vm.refreshEngineInfo()
        if (!vm.useRemote) vm.refreshFunnel()
    }
    val entries = remember(tick, errorsOnly) {
        DebugLog.snapshot().let { l -> if (errorsOnly) l.filter { it.kind == DebugLog.Kind.ERROR } else l }.asReversed()
    }
    Scaffold(topBar = { TopAppBar(title = { Text("Debug") }, navigationIcon = { TextButton(onClick = onBack) { Text("Back") } }) }) { pad ->
        Column(
            Modifier.padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = {
                    val header = "Music Finder debug log\nengine=${if (vm.useRemote) "remote ${vm.baseUrl}" else "on-device"}\n" +
                        "device=${android.os.Build.MODEL} sdk=${android.os.Build.VERSION.SDK_INT}\n${vm.engineInfo}\n----"
                    vm.shareText(ctx, "musicfinder-debug.log", "Music Finder debug log", DebugLog.dump(header))
                }) { Text("Share log") }
                TextButton(onClick = { errorsOnly = !errorsOnly }) { Text(if (errorsOnly) "Show all" else "Errors only") }
                TextButton(onClick = { DebugLog.clear(); tick++ }) { Text("Clear") }
            }
            SelectionContainer { Text(vm.engineInfo, fontFamily = FontFamily.Monospace, fontSize = 11.sp) }
            if (vm.funnelInfo.isNotEmpty()) {
                Text("Candidate funnel", style = MaterialTheme.typography.titleMedium)
                SelectionContainer { Text(vm.funnelInfo, fontFamily = FontFamily.Monospace, fontSize = 11.sp) }
            }
            Text("Recent (${entries.size})", style = MaterialTheme.typography.titleMedium)
            entries.take(300).forEach { e ->
                Text(
                    e.format(),
                    fontFamily = FontFamily.Monospace,
                    fontSize = 11.sp,
                    color = if (e.kind == DebugLog.Kind.ERROR) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onBackground,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun FriendsScreen(vm: RecViewModel, onBack: () -> Unit) {
    val ctx = LocalContext.current
    var tab by remember { mutableIntStateOf(0) }
    LaunchedEffect(Unit) { vm.loadFriends() }
    Scaffold(topBar = { TopAppBar(title = { Text("Friends mode") }, navigationIcon = { TextButton(onClick = onBack) { Text("Back") } }) }) { pad ->
        Column(
            Modifier.padding(pad).padding(16.dp).verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Text(
                "Separate profiles: group reactions only teach the group profile; your own friends-context ratings stay separate. " +
                    "Nothing transfers to your personal recs unless you tap “Also teach my recs”.",
                style = MaterialTheme.typography.bodySmall,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                FilterChip(selected = tab == 0, onClick = { tab = 0 }, label = { Text("Play for friends") })
                FilterChip(selected = tab == 1, onClick = { tab = 1 }, label = { Text("Audition") })
                TextButton(onClick = { vm.loadFriends() }) { Text("Refresh") }
            }
            Text(vm.friendsStatus, style = MaterialTheme.typography.bodySmall)
            val cards = if (tab == 0) vm.friendsQueue else vm.friendsAudition
            if (tab == 1) Text("Audition: unrated candidates ranked by broad appeal and the group's reactions.", style = MaterialTheme.typography.bodySmall)
            cards.forEach { card ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(card.rec.title, style = MaterialTheme.typography.titleMedium)
                        Text(card.rec.artist, color = MaterialTheme.colorScheme.primary)
                        Text(card.rec.why, style = MaterialTheme.typography.bodySmall)
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            Button(onClick = { YtmHandoff.open(ctx, card.rec) }) { Text("Play") }
                            TextButton(onClick = { vm.groupReact(card, "liked") }) { Text("Group: Liked") }
                            TextButton(onClick = { vm.groupReact(card, "didnt_work") }) { Text("Didn't work") }
                            TextButton(onClick = { vm.groupReact(card, "mixed") }) { Text("Mixed") }
                        }
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text("Me here:", style = MaterialTheme.typography.bodySmall, modifier = Modifier.align(Alignment.CenterVertically))
                            listOf("love", "like", "meh", "dislike").forEach { r ->
                                TextButton(onClick = { vm.friendsContextRate(card, r) }) { Text(r.replaceFirstChar { it.uppercase() }) }
                            }
                            TextButton(onClick = { vm.teachPersonal(card, "like") }) { Text("Also teach my recs (Like)") }
                        }
                    }
                }
            }
        }
    }
}
