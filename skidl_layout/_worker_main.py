"""Worker entry point: python -m skidl_layout._worker_main <mode> <in1> <out1> [<in2> <out2> ...]

Launched as a plain subprocess (NOT via multiprocessing), so it never
re-imports the caller's ``__main__`` — this is what makes default-on
parallelism safe for unguarded driver scripts (round-7 WS25).

Each ``(in, out)`` pair: read pickled payload bytes from ``<in>``, run the
mode's worker, write the returned pickled bytes to ``<out>``. Exit 0 on
success; any exception -> traceback on stderr, exit 1 (the parent falls back to
sequential).

Must stay spawn-inert and import-light at module top level (hazard #10): only
``import sys`` here; every ``skidl_layout`` import happens inside ``main`` after
argv parsing.
"""
import sys


def main(argv):
    mode = argv[0]
    from . import parallel

    worker = {
        "refine": parallel.refine_candidate_worker,
        "finalize": parallel.finalize_candidate_worker,
        "full": parallel.plan_candidate_worker,
    }[mode]
    pairs = list(zip(argv[1::2], argv[2::2]))
    for in_path, out_path in pairs:
        with open(in_path, "rb") as f:
            payload = f.read()
        result = worker(payload)
        with open(out_path, "wb") as f:
            f.write(result)


if __name__ == "__main__":
    main(sys.argv[1:])
