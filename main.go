package main

import (
	"encoding/json"
	"context"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"os"
	"io"
	"github.com/gorilla/mux"
	"args"
)

func bty_sh(res http.ResponseWriter, req *http.Request) {
	res.Header().Set("Content-Type", "text/html")
	io.WriteString(
		res,
		"bty.sh",
	)
}

func main() {
	log.Printf("hello there")

	state := State{
		Config: cfg,
	}
	osis_load(cfg, &state.Osis, 0x0)
	bzis_load(cfg, &state.Bzis, 0x0)

	// Initialize the state
	STATE_JSON, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		log.Fatal("err: %v, json.Marshal(%v), ", err, state)
		return
	}
	log.Printf("State below\n%s\n", STATE_JSON)

	// Setup routing
	r := mux.NewRouter()

	r.Handle("/", http.FileServer(http.Dir("assets")))

	r.HandleFunc("/osis/{ident}", OsiHandler)
	r.HandleFunc("/bzis/{ident}", BziHandler)
	r.HandleFunc("/pconfigs/{ident}", PconfigHandler)
	r.HandleFunc("/ptemplates/{ident}", PtemplateHandler)
	r.HandleFunc("/machines/{ident}", MachineHandler)

	http.Handle("/", r)

	server := &http.Server{
		Addr: fmt.Sprintf("%s:%s", cfg.Server.Host, cfg.Server.Port),
	}

	go func() {
		// Graceful shutdown
		sigquit := make(chan os.Signal, 1)
		signal.Notify(sigquit, os.Interrupt, os.Kill)

		sig := <-sigquit
		log.Printf("caught sig: %+v", sig)
		log.Printf("Gracefully shutting down server...")

		if err := server.Shutdown(context.Background()); err != nil {
			log.Printf("Unable to shut down server: %v", err)
		} else {
			log.Println("Server stopped")
		}
	}()

	server.ListenAndServe()
}
