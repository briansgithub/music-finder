from .deezer import DeezerSource
from .musicbrainz import MusicBrainzSource
from .listenbrainz import ListenBrainzSource
from .lastfm import LastFmSource
from .youtube import YouTubeNewPipeSource
from .bandcamp import BandcampSource
from .soundcloud import SoundCloudSource

__all__ = [
    "DeezerSource",
    "MusicBrainzSource",
    "ListenBrainzSource",
    "LastFmSource",
    "YouTubeNewPipeSource",
    "BandcampSource",
    "SoundCloudSource",
]
