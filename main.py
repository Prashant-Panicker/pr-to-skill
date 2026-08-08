"""
CLI entry point. Runs the full pipeline:

  1. collect   -> raw_comments.json          (gh cli, no AI)
  2. analyze   -> notes.json / notes.md      (AI, "note it down")
  3. synthesize-> SKILL.md                   (AI, "create the skill")

Each stage writes its output to disk and can be skipped/resumed if the file
already exists, since scanning big repos + calling an LLM hundreds of times
is slow and you don't want to redo it after a crash or a config tweak.

Usage:
    python main.py --config config.yaml
    python main.py --config config.yaml --skip-collect   # reuse raw_comments.json
    python main.py --config config.yaml --skip-analyze   # reuse notes.json
"""

import argparse
import json
import os
import sys

import yaml

import github_collector as gh
import comment_analyzer as analyzer
import skill_synthesizer as synth
import azure_client
import vector_store
from aws_adapters import S3ArtifactStore


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Mine a person's PR review history into a reusable skill.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--skip-collect", action="store_true", help="reuse existing raw_comments.json")
    parser.add_argument("--skip-analyze", action="store_true", help="reuse existing notes.json")
    parser.add_argument("--search", help="retrieve relevant historical review notes")
    parser.add_argument("--search-limit", type=int, default=5)
    parser.add_argument("--search-repo", help="repository scope for vector retrieval")
    parser.add_argument(
        "--sync-aws-artifacts",
        action="store_true",
        help="bootstrap webhook artifacts in Amazon S3",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    az_cfg = azure_client.resolve_config(cfg["azure_openai"])
    search_cfg = vector_store.resolve_config(
        cfg.get("aws_opensearch", {}),
        embedding_deployment=az_cfg.get("embedding_deployment"),
        embedding_dimensions=az_cfg["embedding_dimensions"],
    )
    if args.search and not search_cfg["enabled"]:
        parser.error("--search requires aws_opensearch.enabled: true")

    if args.search:
        search_repo = args.search_repo
        if search_repo is None:
            if len(cfg.get("repos", [])) != 1:
                parser.error("--search-repo is required when multiple repos are configured")
            search_repo = cfg["repos"][0]
        client = azure_client.get_client(
            endpoint=az_cfg["endpoint"], api_version=az_cfg["api_version"],
            request_timeout=az_cfg["request_timeout"], api_mode=az_cfg["api_mode"],
            max_output_tokens=az_cfg["max_output_tokens"], api_key=az_cfg.get("api_key"),
        )
        store = vector_store.create_store(search_cfg, client)
        results = store.search(
            args.search,
            search_repo,
            limit=args.search_limit,
            reviewer=cfg["person"]["github_username"],
        )
        print(json.dumps(results, indent=2))
        return

    out_dir = cfg["output"]["dir"]
    os.makedirs(out_dir, exist_ok=True)

    username = cfg["person"]["github_username"]
    repos = cfg["repos"]
    updated_after = cfg.get("github", {}).get("updated_after")

    raw_path = os.path.join(out_dir, "raw_comments.json")
    notes_json_path = os.path.join(out_dir, "notes.json")
    notes_md_path = os.path.join(out_dir, "notes.md")
    skill_path = os.path.join(out_dir, "SKILL.md")

    # --- Stage 1: collect ---
    if args.skip_collect and os.path.exists(raw_path):
        print(f"[1/3] Skipping collection, reusing {raw_path}")
        with open(raw_path) as f:
            raw_comments = json.load(f)
    else:
        print(f"[1/3] Collecting closed-PR comments by '{username}' across {len(repos)} repos...")
        collection_workers = cfg.get("github", {}).get("workers", 8)
        comments = gh.collect_all(
            repos, username, updated_after, max_workers=collection_workers
        )
        gh.save_raw(comments, raw_path)
        with open(raw_path) as f:
            raw_comments = json.load(f)
        print(f"      Found {len(raw_comments)} comments. Saved to {raw_path}")

    if not raw_comments:
        print("No comments found for this person in these repos. Nothing to analyze.", file=sys.stderr)
        sys.exit(1)

    # --- Set up Azure OpenAI client ---
    auth_method = "API key" if az_cfg.get("api_key") else "Azure AD (az login)"
    print(f"      Authenticating to Azure OpenAI via {auth_method}")
    client = azure_client.get_client(
        endpoint=az_cfg["endpoint"],
        api_version=az_cfg["api_version"],
        request_timeout=az_cfg["request_timeout"],
        api_mode=az_cfg["api_mode"],
        max_output_tokens=az_cfg["max_output_tokens"],
        api_key=az_cfg.get("api_key"),
    )
    deployment = az_cfg["deployment"]

    # --- Stage 2: analyze ("note it down") ---
    if args.skip_analyze and os.path.exists(notes_json_path):
        print(f"[2/3] Skipping analysis, reusing {notes_json_path}")
        with open(notes_json_path) as f:
            notes = json.load(f)
    else:
        print(f"[2/3] Analyzing {len(raw_comments)} comments with Azure OpenAI ({deployment})...")
        analysis_cfg = cfg.get("analysis", {})
        batch_size = analysis_cfg.get("batch_size", 8)
        max_attempts = analysis_cfg.get("max_attempts", 3)
        analysis_workers = analysis_cfg.get("workers", 4)

        def progress_cb(done, total):
            print(f"      ...{done}/{total}")

        note_objs = analyzer.analyze_all(
            client,
            deployment,
            raw_comments,
            batch_size=batch_size,
            max_attempts=max_attempts,
            max_workers=analysis_workers,
            progress_cb=progress_cb,
        )
        analyzer.save_notes_json(note_objs, notes_json_path)
        analyzer.save_notes_markdown(note_objs, notes_md_path)
        with open(notes_json_path) as f:
            notes = json.load(f)
        print(f"      Wrote {notes_json_path} and {notes_md_path}")

    if search_cfg["enabled"]:
        store = vector_store.create_store(search_cfg, client)
        indexed_count = store.save_notes(notes, username)
        print(f"      Indexed {indexed_count} notes in Amazon OpenSearch")

    # --- Stage 3: synthesize the skill ---
    print(f"[3/3] Synthesizing SKILL.md from {len(notes)} notes...")
    synthesis_cfg = cfg.get("synthesis", {})
    skill_md = synth.synthesize_skill(
        client,
        deployment,
        notes,
        username,
        max_notes_per_call=synthesis_cfg.get("max_notes_per_call", 400),
        max_workers=synthesis_cfg.get("workers", 4),
    )
    synth.save_skill(skill_md, skill_path)
    print(f"      Wrote {skill_path}")

    if args.sync_aws_artifacts:
        import boto3

        bucket = os.environ.get("ARTIFACT_BUCKET")
        if not bucket:
            parser.error("--sync-aws-artifacts requires ARTIFACT_BUCKET")
        artifacts = S3ArtifactStore(
            boto3.client("s3"), bucket, os.environ.get("ARTIFACT_PREFIX", "artifacts")
        )
        for name, path in (
            ("raw_comments.json", raw_path),
            ("notes.json", notes_json_path),
            ("SKILL.md", skill_path),
        ):
            with open(path) as source:
                artifacts.write_text(name, source.read())
        artifacts.write_text(
            "pipeline_state.json",
            json.dumps({"version": 1, "raw_comments": raw_comments, "notes": notes}, indent=2),
        )
        print("      Bootstrapped webhook artifacts in Amazon S3")

    print("\nDone. Pipeline outputs:")
    print(f"  raw comments -> {raw_path}")
    print(f"  notes (json) -> {notes_json_path}")
    print(f"  notes (md)   -> {notes_md_path}")
    print(f"  skill        -> {skill_path}")


if __name__ == "__main__":
    main()
