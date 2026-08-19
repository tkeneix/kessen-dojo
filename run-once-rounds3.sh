SCRIPT_DIR=$(cd $(dirname $0);pwd)
cd ${SCRIPT_DIR}
source .env
uv run kessen run-once --project projects/nk225-yorihike --rounds 3
