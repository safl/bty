BUILD_ROOT := build

.PHONY: build
build:
	rm -fr ${BUILD_ROOT}
	mkdir -p ${BUILD_ROOT}
	go build -o ${BUILD_ROOT}/bty-wui bty.go

start:
	./${BUILD_ROOT}/bty-wui &

stop:
	pkill -f "bty-wui"
