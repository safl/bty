package main

import (
//	"encoding/json"
	"context"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"os"
	"io"
	"github.com/gorilla/mux"
	"github.com/safl/bty/args"
	"github.com/safl/bty/state"
	. "github.com/safl/bty/handlers"
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

	cfg, err := args.Parse()
	if err != nil {
		log.Panic("that's it, we're boned!")
		os.Exit(1)
	}

	curs := state.State{
		Conf: cfg,
	}
	state.LoadOsis(cfg, &curs.Osis, 0x0)
	state.LoadBzis(cfg, &curs.Bzis, 0x0)
	state.LoadPconfigs(cfg, &curs.Pconfigs, 0x0)
	state.LoadPtemplates(cfg, &curs.Ptemplates, 0x0)

	/*
	STATE_JSON, err := json.MarshalIndent(curs, "", "  ")
	if err != nil {
		log.Fatal("err: %v, json.Marshal(%v), ", err, curs)
		return
	}
	log.Printf("State below\n%s\n", STATE_JSON)
	*/

	// Setup routing
	r := mux.NewRouter()

	r.Handle("/", http.FileServer(http.Dir("assets/wui")))

	r.HandleFunc("/osis/{ident}", OsiHandler)
	r.HandleFunc("/bzis/{ident}", BziHandler)
	r.HandleFunc("/pconfigs/{ident}", PconfigHandler)
	r.HandleFunc("/ptemplates/{ident}", PtemplateHandler)
	r.HandleFunc("/machines/{ident}", MachineHandler)

	http.Handle("/", r)

	server := &http.Server{
		Addr: fmt.Sprintf("%s:%d", cfg.Server.Host, cfg.Server.Port),
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

	log.Fatal(server.ListenAndServe())
	log.Printf("done")

	os.Exit(0)
}
