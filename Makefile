BUILD_ROOT := build
BTY_SERVER_HOST := localhost
BTY_SERVER_PORT := 8080
BTY_PATH_OSIS := testdata/osis
BTY_PATH_BZIS := testdata/bzis
BTY_PATH_PCONFIGS := testdata/pxelinux.cfg
BTY_PATH_PTEMPLATES := assets/templates
BTY_PATH_TEMPLATES := assets/templates

.PHONY: build
build:
	rm -fr ${BUILD_ROOT}
	mkdir -p ${BUILD_ROOT}
	go build -o ${BUILD_ROOT}/bty-wui bty.go

start:
	./${BUILD_ROOT}/bty-wui \
		--host ${BTY_SERVER_HOST} \
		--port ${BTY_SERVER_PORT} \
		--osis ${BTY_PATH_OSIS} \
		--bzis ${BTY_PATH_BZIS} \
		--ptemplates ${BTY_PATH_PTEMPLATES} \
		--pconfigs ${BTY_PATH_PCONFIGS}

stop:
	pkill -f bty-wui | true
