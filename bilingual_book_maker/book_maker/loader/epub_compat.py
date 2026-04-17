from __future__ import annotations

from ebooklib import epub


def install_epub_compat_patches() -> None:
    # monkey patch for #173
    def _write_items_patch(obj):
        for item in obj.book.get_items():
            if isinstance(item, epub.EpubNcx):
                obj.out.writestr("%s/%s" % (obj.book.FOLDER_NAME, item.file_name), obj._get_ncx())
            elif isinstance(item, epub.EpubNav):
                obj.out.writestr("%s/%s" % (obj.book.FOLDER_NAME, item.file_name), obj._get_nav(item))
            elif item.manifest:
                obj.out.writestr("%s/%s" % (obj.book.FOLDER_NAME, item.file_name), item.content)
            else:
                obj.out.writestr("%s" % item.file_name, item.content)

    def _check_deprecated(_obj):
        return None

    epub.EpubWriter._write_items = _write_items_patch
    epub.EpubReader._check_deprecated = _check_deprecated


def install_epub_spine_fallback_patch() -> None:
    # compatibility fallback for issue #71
    def _load_spine(obj):
        spine = obj.container.find("{%s}%s" % (epub.NAMESPACES["OPF"], "spine"))
        obj.book.spine = [(item.get("idref"), item.get("linear", "yes")) for item in spine]
        obj.book.set_direction(spine.get("page-progression-direction", None))

    epub.EpubReader._load_spine = _load_spine
