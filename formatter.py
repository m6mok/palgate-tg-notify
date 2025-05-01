from logging import Formatter, LogRecord

from telegram_handler.utils import escape_html


class HtmlFormatter(Formatter):
    """HTML formatter for telegram."""
    parse_mode = 'HTML'

    def format(self, record: LogRecord) -> str:
        super(HtmlFormatter, self).format(record)

        if record.funcName:
            record.funcName = escape_html(str(record.funcName))
        if record.name:
            record.name = escape_html(str(record.name))
        if record.msg:
            record.msg = escape_html(record.getMessage())

        return self._style.format(record)
