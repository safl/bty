BUILD_ROOT := build

build:
	rm -fr ${BUILD_ROOT}
	mkdir -p ${BUILD_ROOT}
	go build bty.go -o ${BUILD_ROOT}/bty-wui

start:
	./${BUILD_ROOT}/bty-wui &

stop:
	pkill -f "bty-wui"
