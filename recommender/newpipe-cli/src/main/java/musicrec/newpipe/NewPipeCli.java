package musicrec.newpipe;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import org.schabi.newpipe.extractor.InfoItem;
import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.StreamingService;
import org.schabi.newpipe.extractor.exceptions.ExtractionException;
import org.schabi.newpipe.extractor.linkhandler.SearchQueryHandler;
import org.schabi.newpipe.extractor.search.SearchExtractor;
import org.schabi.newpipe.extractor.stream.AudioStream;
import org.schabi.newpipe.extractor.stream.StreamInfo;
import org.schabi.newpipe.extractor.stream.StreamInfoItem;

import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * CLI wrapper around NewPipeExtractor.
 * Usage:
 *   newpipe-cli search <query> [--limit N] [--filter music_songs|videos] [--service youtube|soundcloud]
 *   newpipe-cli related <urlOrId> [--service youtube]
 *   newpipe-cli stream <urlOrId> [--service youtube|soundcloud]
 *   newpipe-cli meta <urlOrId> [--service youtube|soundcloud]  (no audio URLs)
 */
public final class NewPipeCli {
    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().create();
    private static final Pattern ITAG = Pattern.compile("[?&]itag=(\\d+)");
    private static final Pattern YT_ID = Pattern.compile("^[A-Za-z0-9_-]{11}$");

    public static void main(final String[] args) {
        if (args.length < 1) usageAndExit();
        try {
            NewPipe.init(new DownloaderImpl());
            final String cmd = args[0].toLowerCase(Locale.ROOT);
            switch (cmd) {
                case "search" -> doSearch(args);
                case "related" -> doRelated(args);
                case "stream" -> doStream(args);
                case "meta" -> doMeta(args);
                default -> usageAndExit();
            }
        } catch (final Exception e) {
            final Map<String, Object> err = new HashMap<>();
            err.put("ok", false);
            err.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            System.out.println(GSON.toJson(err));
            System.exit(1);
        }
    }

    private static void usageAndExit() {
        System.err.println("Usage: newpipe-cli <search|related|stream|meta> ... [--service youtube|soundcloud]");
        System.exit(2);
    }

    private static StreamingService serviceFromArgs(final String[] args, final int start) {
        String name = "youtube";
        for (int i = start; i < args.length; i++) {
            if ("--service".equals(args[i]) && i + 1 < args.length) {
                name = args[i + 1].toLowerCase(Locale.ROOT);
            }
        }
        return switch (name) {
            case "soundcloud", "sc" -> ServiceList.SoundCloud;
            default -> ServiceList.YouTube;
        };
    }

    private static void doSearch(final String[] args) throws ExtractionException, IOException {
        if (args.length < 2) usageAndExit();
        int limit = 10;
        String filter = "";
        final StringBuilder q = new StringBuilder();
        for (int i = 1; i < args.length; i++) {
            if ("--limit".equals(args[i]) && i + 1 < args.length) {
                limit = Integer.parseInt(args[++i]);
            } else if ("--filter".equals(args[i]) && i + 1 < args.length) {
                filter = args[++i];
            } else if ("--service".equals(args[i]) && i + 1 < args.length) {
                i++; // skip
            } else {
                if (q.length() > 0) q.append(' ');
                q.append(args[i]);
            }
        }
        final StreamingService service = serviceFromArgs(args, 1);
        final SearchQueryHandler qh;
        if (filter.isEmpty()) {
            qh = service.getSearchQHFactory().fromQuery(q.toString());
        } else {
            qh = service.getSearchQHFactory().fromQuery(q.toString(), List.of(filter), "");
        }
        final SearchExtractor extractor = service.getSearchExtractor(qh);
        extractor.fetchPage();
        final List<Map<String, Object>> items = new ArrayList<>();
        for (final InfoItem item : extractor.getInitialPage().getItems()) {
            if (items.size() >= limit) break;
            final Map<String, Object> m = new HashMap<>();
            m.put("name", item.getName());
            m.put("url", item.getUrl());
            m.put("type", item.getInfoType().name());
            if (item instanceof StreamInfoItem si) {
                m.put("uploader", si.getUploaderName());
                m.put("duration", si.getDuration());
                m.put("view_count", si.getViewCount());
            }
            items.add(m);
        }
        final Map<String, Object> out = new HashMap<>();
        out.put("ok", true);
        out.put("service", service.getServiceInfo().getName());
        out.put("query", q.toString());
        out.put("filter", filter);
        out.put("items", items);
        System.out.println(GSON.toJson(out));
    }

    private static void doRelated(final String[] args) throws ExtractionException, IOException {
        if (args.length < 2) usageAndExit();
        final String url = normalizeUrl(args[1], serviceFromArgs(args, 1));
        final StreamInfo info = StreamInfo.getInfo(url);
        final List<Map<String, Object>> items = new ArrayList<>();
        for (final InfoItem item : info.getRelatedItems()) {
            final Map<String, Object> m = new HashMap<>();
            m.put("name", item.getName());
            m.put("url", item.getUrl());
            m.put("type", item.getInfoType().name());
            if (item instanceof StreamInfoItem si) {
                m.put("uploader", si.getUploaderName());
                m.put("duration", si.getDuration());
            }
            items.add(m);
        }
        final Map<String, Object> out = new HashMap<>();
        out.put("ok", true);
        out.put("url", url);
        out.put("title", info.getName());
        out.put("uploader", info.getUploaderName());
        out.put("related", items);
        System.out.println(GSON.toJson(out));
    }

    private static void doStream(final String[] args) throws ExtractionException, IOException {
        if (args.length < 2) usageAndExit();
        final StreamingService svc = serviceFromArgs(args, 1);
        final String url = normalizeUrl(args[1], svc);
        final StreamInfo info = StreamInfo.getInfo(url);
        final List<Map<String, Object>> audio = new ArrayList<>();
        for (final AudioStream s : info.getAudioStreams()) {
            final Map<String, Object> m = new HashMap<>();
            final String content = s.getContent();
            m.put("url", content);
            m.put("format", s.getFormat() == null ? null : s.getFormat().getName());
            m.put("mime", s.getFormat() == null ? null : s.getFormat().getMimeType());
            m.put("average_bitrate", s.getAverageBitrate());
            m.put("delivery", s.getDeliveryMethod() == null ? null : s.getDeliveryMethod().name());
            final Matcher mt = ITAG.matcher(content == null ? "" : content);
            if (mt.find()) {
                m.put("itag", mt.group(1));
            }
            // Soft Clones: callers should prefer audio-only; mark clearly
            m.put("is_audio_only", true);
            audio.add(m);
        }
        final Map<String, Object> out = new HashMap<>();
        out.put("ok", true);
        out.put("url", url);
        out.put("title", info.getName());
        out.put("uploader", info.getUploaderName());
        out.put("uploader_url", info.getUploaderUrl());
        out.put("duration", info.getDuration());
        out.put("category", info.getCategory());
        out.put("tags", info.getTags());
        out.put("service", info.getService().getServiceInfo().getName());
        out.put("audio_streams", audio);
        System.out.println(GSON.toJson(out));
    }

    /** Metadata only (category/uploader/duration/title) — still uses StreamInfo extraction. */
    private static void doMeta(final String[] args) throws ExtractionException, IOException {
        if (args.length < 2) usageAndExit();
        final StreamingService svc = serviceFromArgs(args, 1);
        final String url = normalizeUrl(args[1], svc);
        final StreamInfo info = StreamInfo.getInfo(url);
        final Map<String, Object> out = new HashMap<>();
        out.put("ok", true);
        out.put("url", url);
        out.put("title", info.getName());
        out.put("uploader", info.getUploaderName());
        out.put("uploader_url", info.getUploaderUrl());
        out.put("duration", info.getDuration());
        out.put("category", info.getCategory());
        out.put("tags", info.getTags());
        out.put("service", info.getService().getServiceInfo().getName());
        System.out.println(GSON.toJson(out));
    }

    private static String normalizeUrl(final String input, final StreamingService service) {
        final String s = input.trim();
        if (s.startsWith("http://") || s.startsWith("https://")) {
            // Canonicalize YouTube watch URLs: strip t/start/list/pp/si/feature
            if (s.contains("youtu")) {
                final Matcher m = Pattern.compile("(?:v=|/shorts/|/embed/|youtu\\.be/)([A-Za-z0-9_-]{11})").matcher(s);
                if (m.find()) {
                    return "https://www.youtube.com/watch?v=" + m.group(1);
                }
            }
            return s;
        }
        if (YT_ID.matcher(s).matches()) {
            return "https://www.youtube.com/watch?v=" + s;
        }
        return s;
    }
}
