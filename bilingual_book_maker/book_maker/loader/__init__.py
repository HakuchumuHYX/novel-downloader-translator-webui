from __future__ import annotations

from importlib import import_module


class LazyBookLoaderRegistry(dict):
    def _resolve(self, key):
        value = dict.__getitem__(self, key)
        if not isinstance(value, tuple):
            return value
        module_name, attr_name = value
        loader = getattr(import_module(module_name), attr_name)
        dict.__setitem__(self, key, loader)
        return loader

    def __getitem__(self, key):
        return self._resolve(key)

    def get(self, key, default=None):
        if key not in self:
            return default
        return self._resolve(key)

    def items(self):
        for key in dict.keys(self):
            yield key, self._resolve(key)

    def values(self):
        for key in dict.keys(self):
            yield self._resolve(key)


BOOK_LOADER_DICT = LazyBookLoaderRegistry(
    {
        "epub": ("book_maker.loader.epub_loader", "EPUBBookLoader"),
        "txt": ("book_maker.loader.txt_loader", "TXTBookLoader"),
        "srt": ("book_maker.loader.srt_loader", "SRTBookLoader"),
        "md": ("book_maker.loader.md_loader", "MarkdownBookLoader"),
        "pdf": ("book_maker.loader.pdf_loader", "PDFBookLoader"),
    }
)
