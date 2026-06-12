import re
import backoff
import inspect
import logging
from copy import copy

from bs4 import BeautifulSoup

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


@backoff.on_exception(
    backoff.expo,
    Exception,
    max_tries=5,
    on_backoff=lambda details: logger.warning(f"retry backoff: {details}"),
    on_giveup=lambda details: logger.warning(f"retry abort: {details}"),
    jitter=None,
)
def translate_model_with_backoff(translate_model, text, context_flag=False):
    translate = translate_model.translate
    try:
        signature = inspect.signature(translate)
    except (TypeError, ValueError):
        return translate(text)
    parameters = signature.parameters
    accepts_context_flag = "context_flag" in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_context_flag:
        return translate(text, context_flag=context_flag)
    return translate(text)


def _soup_root(node) -> BeautifulSoup | None:
    current = node
    while getattr(current, "parent", None) is not None:
        current = current.parent
    return current if hasattr(current, "new_tag") else None


def _replace_tag_text_preserving_breaks(tag, text: str, soup: BeautifulSoup | None = None) -> None:
    tag.clear()
    soup = soup or _soup_root(tag)
    lines = str(text or "").split("\n")
    for idx, line in enumerate(lines):
        if idx:
            if soup is not None:
                tag.append(soup.new_tag("br"))
            else:
                tag.append("\n")
        tag.append(line)


class EPUBBookLoaderHelper:
    def __init__(
        self, translate_model, accumulated_num, translation_style, context_flag
    ):
        self.translate_model = translate_model
        self.accumulated_num = accumulated_num
        self.translation_style = translation_style
        self.context_flag = context_flag

    def insert_trans(self, p, text, translation_style="", single_translate=False):
        if text is None:
            text = ""
        if (
            p.string is not None
            and p.string.replace(" ", "").strip() == text.replace(" ", "").strip()
        ):
            return
        new_p = copy(p)
        _replace_tag_text_preserving_breaks(new_p, text, soup=_soup_root(p))
        if translation_style != "":
            new_p["style"] = translation_style
        p.insert_after(new_p)
        if single_translate:
            p.extract()

    def translate_with_backoff(self, text, context_flag=False):
        return translate_model_with_backoff(self.translate_model, text, context_flag)

    def deal_new(self, p, wait_p_list, single_translate=False):
        self.deal_old(wait_p_list, single_translate, self.context_flag)
        self.insert_trans(
            p,
            shorter_result_link(self.translate_with_backoff(p.text, self.context_flag)),
            self.translation_style,
            single_translate,
        )

    def deal_old(self, wait_p_list, single_translate=False, context_flag=False):
        if not wait_p_list:
            return

        result_txt_list = self.translate_model.translate_list(wait_p_list)

        for i in range(len(wait_p_list)):
            if i < len(result_txt_list):
                p = wait_p_list[i]
                self.insert_trans(
                    p,
                    shorter_result_link(result_txt_list[i]),
                    self.translation_style,
                    single_translate,
                )

        wait_p_list.clear()


url_pattern = r"(http[s]?://|www\.)+(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+"


def is_text_link(text):
    return bool(re.compile(url_pattern).match(text.strip()))


def is_text_tail_link(text, num=80):
    text = text.strip()
    pattern = r".*" + url_pattern + r"$"
    return bool(re.compile(pattern).match(text)) and len(text) < num


def shorter_result_link(text, num=20):
    match = re.search(url_pattern, text)

    if not match or len(match.group()) < num:
        return text

    return re.compile(url_pattern).sub("...", text)


def is_text_source(text):
    return text.strip().startswith("Source: ")


def is_text_list(text, num=80):
    text = text.strip()
    return re.match(r"^Listing\s*\d+", text) and len(text) < num


def is_text_figure(text, num=80):
    text = text.strip()
    return re.match(r"^Figure\s*\d+", text) and len(text) < num


def is_text_digit_and_space(s):
    for c in s:
        if not c.isdigit() and not c.isspace():
            return False
    return True


def is_text_isbn(s):
    pattern = r"^[Ee]?ISBN\s*\d[\d\s]*$"
    return bool(re.match(pattern, s))


def not_trans(s):
    return any(
        [
            is_text_link(s),
            is_text_tail_link(s),
            is_text_source(s),
            is_text_list(s),
            is_text_figure(s),
            is_text_digit_and_space(s),
            is_text_isbn(s),
        ]
    )
