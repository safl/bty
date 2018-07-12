package main

import (
//	"encoding/json"
	"context"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"os"
	"github.com/gorilla/mux"
	"github.com/safl/bty/args"
	"github.com/safl/bty/state"
	. "github.com/safl/bty/handlers"
)

func main() {
	log.Printf("Welcome to the jungle we've got fun and games")

	cfg, err := args.Parse()
	if err != nil {
		log.Panic("can't parse CLI args; that's it, we're boned!")
		os.Exit(1)
	}

	curs, err := state.Initialize(cfg)
	if (err != nil) {
		log.Panic("can't initialzie state; that's it, we're boned!")
		os.Exit(1)
	}

	//log.Printf("curs: %v", curs)
	for _, tmpl := range curs.Ptemplates {
		log.Printf("tmpl.checksum: %v", tmpl.Finf.Checksum)
	}

	for _, pconf := range curs.Pconfigs {
		log.Printf("pconf.checksum: %v", pconf.Finf.Checksum)
	}

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

	sig_chan := make(chan os.Signal, 1)
	signal.Notify(sig_chan)		// Just like Pokemon, gotta catch em all
	go func() {
		for sig := range sig_chan {
			log.Printf("caught sig: %+v", sig)

			// TODO: make sure state is not being modified

			err := server.Shutdown(context.Background())
			if err != nil {
				log.Printf("Unable to shut down server: %v", err)
			} else {
				log.Println("Server stopped")
			}
		}
	}()

	log.Fatal(server.ListenAndServe())

	log.Printf("Its gonna bring you down, ha!")
	os.Exit(0)
}

