from __future__ import annotations

import html
import re
from operator import attrgetter

import tinycss
from srt import Subtitle

from subby.converters.base import BaseConverter
from subby.subripfile import SubRipFile
from subby.utils.time import timedelta_from_timestamp

CUE_TAG = re.compile(r'<(/?)([^\s/>]+)([^>]*)>')
TIME_RE = re.compile(r'\d --> \d',)
SKIP_RE = re.compile(r'^(?:WEBVTT|NOTE|/\*|REGION|X-TIMESTAMP-MAP)')
KNOWN_TAGS = ('b', 'c', 'i', 'lang', 'rp', 'rt', 'ruby', 'u', 'v')

STYLE_TYPE = dict[frozenset[str], dict[str, str]]


class WebVTTConverter(BaseConverter):
    """WebVTT subtitle converter"""

    def parse(self, stream):
        srt = SubRipFile()
        looking_for_text = False
        looking_for_style = False
        text: list[str] = []
        line_number = 1
        styles: STYLE_TYPE = {}
        current_style: list[str] = []

        css_parser = tinycss.make_parser('page3')

        for line in stream:
            # As our stream is bytes we have to deal with line breaks here
            line = line.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').strip()

            # Skip processing any unnecessary lines
            if SKIP_RE.match(line):
                continue

            # Empty line separates cues
            if line == '':
                # Parse current style
                if looking_for_style:
                    stylesheet = css_parser.parse_stylesheet('\n'.join(current_style))
                    current_style.clear()
                    for rule in stylesheet.rules:
                        ft = next((e for e in rule.selector if e.type == 'FUNCTION'), None)
                        if not ft:
                            continue
                        # Key by all style names to handle compound selectors
                        # https://github.com/pbs/pycaption/commit/77ba1b0
                        key = frozenset(t.value for t in ft.content if t.type == 'IDENT')
                        if not key:
                            continue
                        styles[key] = {}
                        for dec in rule.declarations:
                            styles[key][dec.name] = dec.value.as_css()

                    looking_for_style = False

                # Keep looking for text if last line has none
                # this will only happen if there's an unexpected line break
                if not text:
                    continue

                srt[-1].content = '\n'.join(text)
                text = []
                looking_for_text = False

            # Check for style start
            elif line == 'STYLE':
                looking_for_style = True

            # Check for style content
            elif looking_for_style:
                current_style.append(line)

            # Check for time line
            elif TIME_RE.search(line):
                # Time line should always cause a line split, even without a separating new line
                if looking_for_text and text and srt:
                    srt[-1].content = '\n'.join(text)
                    text = []

                parts = line.strip().split()
                inline_text = ''

                # Handle misformed lines (likely a poor SRT->VTT conversion)
                # e.g. 637 01:11:38,875 --> 01:11:41,500. Capita.
                # Checks line number to avoid catching false positives
                if parts[0].isdigit() and int(parts[0]) == line_number:
                    parts = parts[1:]
                    inline_text = ' '.join(parts[3:])

                position = self._get_position([p for p in parts[3:] if ':' in p and '-->' not in p])

                start, _, end, *_ = parts
                # Fix short timecodes (no hour)
                if start.count(':') == 1:
                    start = f'00:{start}'
                if end.count(':') == 1:
                    end = f'00:{end}'

                srt.append(Subtitle(
                    index=line_number,
                    start=timedelta_from_timestamp(start.strip('.')),
                    end=timedelta_from_timestamp(end.strip('.')),
                    content=inline_text,
                    # misuse this field to temporarily hold position
                    proprietary=position if position is not None else 100  # pyright: ignore[reportArgumentType]
                ))

                looking_for_text = True
                line_number += 1

            # Append text if we're inside a line
            elif looking_for_text:
                text.append(line.strip())

        # Add any leftover text to the last line
        if text:
            srt[-1].content += '\n'.join(text)

        # Sort lines with identical timecodes by position
        # Some subtitles have them in an incorrect order, but display correctly due to positioning
        srt.sort(key=attrgetter('start', 'end', 'proprietary'))

        for line in srt:
            # Convert VTT tags to SRT
            if '<' in line.content:
                line.content = self._convert_cue_tags(line.content, styles)

            # Unescape HTML entities
            line.content = html.unescape(line.content)

            # Set \an8 tag if position is below 25
            # (value taken from Subtitle Edit)
            position = line.proprietary
            if position is not None and position < 25:
                line.content = '{\\an8}' + line.content

            line.proprietary = ''  # remove misused field

        return srt

    @staticmethod
    def _get_position(cue_settings: list[str]) -> float | None:
        """
        Parses list of cue settings and extracts position offset as a float
        Line number based offset and alignment strings are ignored

        https://www.w3.org/TR/webvtt1/#webvtt-line-cue-setting
        """
        if not cue_settings or cue_settings == ['None']:
            return None

        position = None
        for key, val in (pos.split(':') for pos in cue_settings if pos.count(':') == 1):
            if key == 'line' and val and (val := val.split(',')[0])[-1] == '%':
                position = float(val[:-1])
                break
            elif key == 'line' and val and val == '0':
                position = 0
                break

        return position

    def _convert_cue_tags(self, content: str, styles: STYLE_TYPE) -> str:
        """
        Converts WebVTT cue text tags to SRT

        An end tag only closes the current node when its tag name matches,
        </ruby> auto-closes an unclosed <rt>, unmatched end tags are ignored,
        and tags left open close at the end of the cue.
        """
        out = []

        T = tuple[str, bool, list[str]]
        stack: list[T] = []

        def emit(text: str):
            if stack:
                stack[-1][2].append(text)
                return
            return out.append(text)

        def flush(span: T) -> str:
            name, italic, buffer = span
            text = ''.join(buffer)
            if name == 'rt':
                text = f'({text})'
            elif name == 'rp':
                text = ''
            if italic and text.strip():
                text = f'<i>{text}</i>'
            return text

        cursor = 0
        for tag in CUE_TAG.finditer(content):
            emit(content[cursor:tag.start()])
            cursor = tag.end()

            closing, name = tag[1], tag[2]

            if closing:
                # Closing tags may repeat the class name (</c.magenta>)
                base = name.split('.', 1)[0]

                if stack and stack[-1][0] == base:
                    emit(flush(stack.pop()))
                # </ruby> auto-closes an unclosed <rt>
                elif base == 'ruby' and len(stack) > 1 \
                        and stack[-1][0] == 'rt' and stack[-2][0] == 'ruby':
                    emit(flush(stack.pop()))
                    emit(flush(stack.pop()))
            else:
                base, _, classes = name.partition('.')
                if base not in KNOWN_TAGS:
                    continue

                italic = base == 'i' or self._is_italic(set(classes.split('.')), styles)
                stack.append((base, italic, []))

        emit(content[cursor:])

        # Close any leftover tags
        while stack:
            emit(flush(stack.pop()))

        return ''.join(out)

    @staticmethod
    def _is_italic(classes: set[str], styles: STYLE_TYPE) -> bool:
        """Determines if any of the specified classes should be italicized"""
        # "font-style_italic" class name is an out of specs hack
        return 'font-style_italic' in classes or any(
            rules.issubset(classes) and values.get('font-style') == 'italic'
            for rules, values in styles.items()
        )
