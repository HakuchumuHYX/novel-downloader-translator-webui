import argparse

import aiohttp

from cli_support import (
    build_download_options,
    build_parser,
    postprocess_download_output,
    print_run_summary,
    validate_args,
)
from downloader import DownloadJob, DownloadOptions


def parse_args() -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    return args


def main():
    args = parse_args()
    options: DownloadOptions = build_download_options(args)
    job = DownloadJob(options)
    result, novel_dir = job.run()
    print_run_summary(result, novel_dir)
    postprocess_download_output(args, result, novel_dir)


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
