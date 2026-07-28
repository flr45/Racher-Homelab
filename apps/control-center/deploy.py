import argparse
import os
import sys

from config import Config
from services.database_service import open_database
from services.docker_service import (
    cleanup_rollout_backups,
    inspect_rollout_container,
    pull_rollout_image,
    replace_rollout_container,
)
from services.rollout_service import RolloutError, create_rollout, execute_rollout


def database():
    return open_database(Config.DATA_ROOT, Config.DATABASE_PATH)


def parser():
    command = argparse.ArgumentParser(description="Kontrolleret Racher OS deployment")
    command.add_argument("container")
    command.add_argument("image")
    command.add_argument("--actor", default="local-cli")
    command.add_argument("--confirm", required=True)
    command.add_argument("--timeout", type=int, default=120)
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    if os.getenv("DEPLOYMENT_ACTIONS_ENABLED", "false").lower() != "true":
        print("Deployment-handlinger er deaktiveret.", file=sys.stderr)
        return 2
    if args.confirm != args.container:
        print("--confirm skal være identisk med containernavnet.", file=sys.stderr)
        return 2
    if args.container in Config.PROTECTED_CONTAINERS:
        print("Containeren er beskyttet mod rollout.", file=sys.stderr)
        return 2
    if args.timeout < 10 or args.timeout > 900:
        print("Timeout skal være mellem 10 og 900 sekunder.", file=sys.stderr)
        return 2

    try:
        rollout = create_rollout(args.container, args.image, args.actor, database)
        result = execute_rollout(
            rollout["id"],
            database,
            pull_image=pull_rollout_image,
            replace_container=replace_rollout_container,
            inspect_container=inspect_rollout_container,
            cleanup_backups=cleanup_rollout_backups,
            timeout_seconds=args.timeout,
        )
    except RolloutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Rollout {result['id']}: {result['status']} · {result['container_name']} · {result['target_image']}"
    )
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
