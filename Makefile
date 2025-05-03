TARGET = palgate-tg-notify

PROTO_DIR = ./protos
MODEL_DIR = ./models

ENV_FILE = ./.dev.env

PROTO_SOURCES = $(wildcard ${PROTO_DIR}/*.proto)
MODEL_SOURCES = $(wildcard ${MODEL_DIR}/*.py)

export PATH := $(HOME)/.local/bin:$(PATH)


.PHONY : ensure-uv

all : install proto mypy

run : docker-dev


ensure-uv :
	@{ \
		if command -v uv >/dev/null 2>&1; then \
			exit 0; \
		elif command -v curl >/dev/null 2>&1; then \
			sh -c "$$(curl -fsSL https://astral.sh/uv/install.sh)"; \
		elif command -v wget >/dev/null 2>&1; then \
			sh -c "$$(wget -qO- https://astral.sh/uv/install.sh)"; \
		else \
			echo "Installation failed. Neither curl nor wget are installed"; \
			exit 1; \
		fi \
	}


install : ensure-uv
	uv sync

proto ${MODEL_SOURCES} : ${PROTO_SOURCES}
	mkdir -p ${MODEL_DIR}
	export PATH=$$PWD/.venv/bin:$$PATH; \
	protoc \
		--proto_path=${PROTO_DIR} \
		--pydantic_out=${MODEL_DIR} \
		${PROTO_SOURCES}

mypy : ${MODEL_SOURCES}
	uv run mypy .

docker-dev : ${ENV_FILE}
	docker build -t ${TARGET} .
	docker rm -f ${TARGET}-container
	docker run --env-file ${ENV_FILE} --name ${TARGET}-container ${TARGET}:latest

clean :
	rm -rf .venv ${MODEL_DIR} .mypy_cache
	uv clean
