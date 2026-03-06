import argparse
from pathlib import Path

import aiohttp

from converters import convert_directory_txt_to_epub, convert_single_txt_to_epub, merge_txt_files
from downloader import DownloadJob, DownloadOptions
from downloader.utils import normalize_input_url, sanitize_filename


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="multi-site novel downloader (syosetu/novel18/kakuyomu)"
    )

    parser.add_argument("--url", default="", help="Full novel URL")
    parser.add_argument(
        "--novel_id",
        default="",
        help="Legacy syosetu novel id, e.g. n4350im (kept for compatibility)",
    )
    parser.add_argument(
        "--site",
        default="auto",
        choices=["auto", "syosetu", "novel18", "kakuyomu"],
        help="Site selector. auto detects from --url",
    )
    parser.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "node", "native"],
        help="Backend selector. auto prefers node and falls back to native (syosetu only)",
    )

    parser.add_argument(
        "--save-format",
        default="txt",
        choices=["txt", "epub"],
        help="Output format",
    )
    parser.add_argument("--proxy", default="", help="Proxy URL")
    parser.add_argument("--output-dir", default="./downloads", help="Output directory")
    parser.add_argument(
        "--record-chapter-number",
        default=False,
        nargs="?",
        const=True,
        type=_parse_bool,
        help="Record chapter number in output text",
    )
    parser.add_argument(
        "--merge-all",
        action="store_true",
        help="Merge all generated txt files into one full-book txt",
    )
    parser.add_argument(
        "--merged-name",
        default="",
        help="Merged output file name (without extension preferred). Empty = use novel title",
    )

    parser.add_argument("--cookie", default="", help="Cookie header string")
    parser.add_argument("--cookie-file", default="", help="Cookie file path")
    parser.add_argument(
        "--paid-policy",
        default="skip",
        choices=["skip", "fail", "metadata"],
        help="How to handle restricted/paid episodes",
    )

    parser.add_argument(
        "--rate-limit",
        type=float,
        default=1.0,
        help="Delay between backend calls/retries (seconds)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retry count per backend",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Command/request timeout in seconds",
    )

    args = parser.parse_args()

    if not args.url and not args.novel_id:
        parser.error("Either --url or --novel_id must be provided")

    return args


def main():
    args = parse_args()

    url = normalize_input_url(args.url, args.novel_id, args.site)

    options = DownloadOptions(
        url=url,
        site=args.site,
        backend=args.backend,
        proxy=args.proxy,
        output_dir=Path(args.output_dir),
        save_format=args.save_format,
        record_chapter_number=args.record_chapter_number,
        merge_all=args.merge_all,
        merged_name=args.merged_name,
        cookie=args.cookie,
        cookie_file=args.cookie_file,
        paid_policy=args.paid_policy,
        rate_limit=max(0.0, args.rate_limit),
        retries=max(0, args.retries),
        timeout=max(1, args.timeout),
    )

    job = DownloadJob(options)
    result, novel_dir = job.run()

    print(f"Backend: {result.backend}")
    print(f"Site: {result.site}")
    print(f"Title: {result.meta.title}")
    print(f"Output dir: {novel_dir}")
    print(f"Chapters: {len(result.chapters)}")

    txt_files = list(novel_dir.glob("*.txt"))
    if not txt_files:
        print("No txt files were produced. Check manifest.json for details.")
        return

    if args.merge_all:
        merged_filename = (args.merged_name or "").strip()
        if not merged_filename or merged_filename == "full_book":
            merged_filename = sanitize_filename(result.meta.title or "", default="full_book")
        if not merged_filename.endswith(".txt"):
            merged_filename = f"{merged_filename}.txt"
        merged_txt_path = merge_txt_files(str(novel_dir), merged_filename)
        print(f"Merged txt saved: {merged_txt_path}")

        if args.save_format == "epub":
            convert_single_txt_to_epub(merged_txt_path)
    else:
        if args.save_format == "epub":
            convert_directory_txt_to_epub(str(novel_dir))


def _run_cli() -> None:
    try:
        main()
    except (
        ConnectionResetError,
        aiohttp.ServerDisconnectedError,
        aiohttp.ClientConnectorError,
    ):
        import traceback

        print(traceback.format_exc())
        print("check your network or proxy")


if __name__ == "__main__":
    _run_cli()
