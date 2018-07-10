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

	http.Handle(
		"/assets",
		http.StripPrefix(
			"/assets/",
			http.FileServer(http.Dir("assets")),
		),
	)

	http.HandleFunc("/bty.sh", bty_sh)

	http.HandleFunc("/hw", bty_hw)

	http.ListenAndServe(":8080", nil)

}
