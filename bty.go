package main

import "net/http"
import "io"

func bty_hw(res http.ResponseWriter, req *http.Request) {
	res.Header().Set("Content-Type", "text/html")
	io.WriteString(
		res,
		"hello there",
	)

}

func bty_sh(res http.ResponseWriter, req *http.Request) {
	res.Header().Set("Content-Type", "text/html")
	io.WriteString(
		res,
		"bty.sh",
	)

}

func main() {

	http.HandleFunc("/bty.sh", bty_sh)

	http.HandleFunc("/hw", bty_hw)

	http.Handle(
		"/",
		http.StripPrefix(
			"/",
			http.FileServer(http.Dir("assets")),
		),
	)

	http.ListenAndServe(":8080", nil)

}
