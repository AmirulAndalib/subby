from html.parser import HTMLParser

from srt import Subtitle

from subby.converters.base import BaseConverter
from subby.subripfile import SubRipFile
from subby.utils.time import timedelta_from_ms


class SAMIConverter(BaseConverter):
    """SAMI subtitle converter"""

    def parse(self, stream):
        return _SAMIConverter(stream.read().decode('utf-8-sig')).srt


# Internal converter class as we inherit from HTMLParser
class _SAMIConverter(HTMLParser):
    def __init__(self, subtitle):
        super().__init__()
        self.lines = []
        self.tags = []

        self.srt = SubRipFile([])

        self.feed(self._correct_tags(subtitle))
        self._convert()

    def handle_starttag(self, tag, attrs):
        if tag == 'sync':
            data = {'text': ''}
            data.update(attrs)
            self.lines.append(data)

        self.tags.append({'name': tag})

    def handle_data(self, data):
        # Skip data prepended before the document
        if not self.tags:
            return

        last_tag = self.tags[-1]['name']
        if last_tag == 'br':
            self.lines[-1]['text'] += '\n'
            return

        if last_tag == 'i' and data.strip():
            self.lines[-1]['text'] += f'<i>{data}</i>'
            return

        if last_tag != 'sync' and self.lines:
            self.lines[-1]['text'] += data

    def _convert(self):
        for num, line in enumerate(self.lines):
            # Use empty lines as the end of previous line
            if not line.get('text', '').strip():
                end_time = float(line['start'])
                if self.srt:
                    self.srt[-1].end = timedelta_from_ms(end_time)
                continue

            if not line.get('start'):
                line['start'] = 0

            if not line.get('end'):
                # Arbitrarily set duration to 4s if end time not present
                line['end'] = float(line['start']) + 4000

            srt_line = Subtitle(
                index=num,
                start=timedelta_from_ms(float(line['start'])),
                end=timedelta_from_ms(float(line['end'])),
                content=line['text'].strip()
            )
            self.srt.append(srt_line)

    @staticmethod
    def _correct_tags(data):
        data = data.replace('<i/>', '<i>')
        data = data.replace(';>', '>')
        data = data.replace('<br>', '\n')
        data = data.replace('<br/>', '\n')
        data = data.replace('<br >', '\n')
        return data
