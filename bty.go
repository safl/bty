package main

import (
	"encoding/json"
	"context"
	"flag"
	"path/filepath"
	"time"
	"fmt"
	"log"
	"net/http"
	"os/signal"
	"os"
	"io"
	"github.com/gorilla/mux"
)

type Server struct {
	Host		string	`json:"host"`
	Port		int	`json:"port"`
}

type Locs struct {
	Osis		string	`json:"osis"`
	Bzis		string	`json:"bzis"`
	Pconfigs	string	`json:"pconfigs"`
	Ptemplates	string	`json:"ptemplates"`
	Templates	string	`json:"templates"`
}

type Patterns struct {
	OsiExt		string	`json:"osi_ext"`
}

type Config struct {
	Server		Server		`json:"server"`
	Locs		Locs		`json:"locs"`
	Patterns	Patterns	`json:"patterns"`
}

type Osi struct {
	Fname		string
	Fsize		int
	Fmode		os.FileMode
	Ftmod		time.Time

	Checksum	string
}

type Bzi struct {
	Fname		string
	Fsize		int
	Fmode		os.FileMode
	Ftmod		time.Time

	Checksum	string
}

type Pconfig struct {
	Fname		string
	Fsize		int
	Fmode		os.FileMode
	Ftmod		time.Time

	Checksum	string
	Content		string
}

type Ptemplate struct {
	Fname		string
	Fsize		int
	Fmode		os.FileMode
	Ftmod		time.Time

	Checksum	string
	Content		string

	Plabels		[]string
}

type Machine struct {
	hwa		string
	Hostname	string
	managed		string
	osi		Osi
	bzi		Osi
	plabel		string
	ptemplate	Ptemplate
}

type State struct {
	config		Config

	Osis		[]Osi
	Bzis		[]Bzi
	Pconfigs	[]Pconfig
	Ptemplates	[]Ptemplate
	machines	[]Machine
}

var LOG = log.New(os.Stdout, "", log.Lshortfile)
var CFG = Config {}

//
// Load Operating System Disk Images from the given path
//
func osis_load(path string, osis []Osi, flags int) {

	var fnames, err = filepath.Glob(path + CFG.Patterns.OsiExt)
	if err != nil {
		LOG.Printf("err: %v", err)
		return
	}

	for _, osi_fname := range fnames {
		LOG.Printf("juice: %v", osi_fname)
	}
}


func bty_sh(res http.ResponseWriter, req *http.Request) {
	res.Header().Set("Content-Type", "text/html")
	io.WriteString(
		res,
		"bty.sh",
	)
}

func BziHandler(resp http.ResponseWriter, req *http.Request) {
	LOG.Printf("HUHA")

	switch(req.Method) {
	
	}
}

func OsiHandler(resp http.ResponseWriter, req *http.Request) {
	LOG.Printf("HUHA")

	switch(req.Method) {
	
	}
}

func PconfigHandler(resp http.ResponseWriter, req *http.Request) {
	LOG.Printf("HUHA")

	switch(req.Method) {
	
	}
}

func PtemplateHandler(resp http.ResponseWriter, req *http.Request) {
	LOG.Printf("HUHA")

	switch(req.Method) {
	
	}
}

func MachineHandler(resp http.ResponseWriter, req *http.Request) {
	LOG.Printf("HUHA")

	switch(req.Method) {
	
	}
}

func main() {
	LOG.Printf("hello there")

	// Setup default config here
	CFG.Server.Host = "localhost"
	CFG.Server.Port = 80

	CFG.Locs.Osis = "/srv/osis"
	CFG.Locs.Bzis = "/srv/tftp/bzi"

	CFG.Locs.Pconfigs = "/srv/bty/pconfigs"
	CFG.Locs.Ptemplates = "/srv/bty/ptemplates"
	CFG.Locs.Templates = "/srv/bty/templates"

	CFG.Patterns.OsiExt = "/*qcow2"

	// Overwrite default configuration with CLI arguments
	flag.StringVar(
		&CFG.Server.Host,
		"host",
		CFG.Server.Host,
		"Hostname / Address to listen on",
	)
	flag.IntVar(
		&CFG.Server.Port,
		"port",
		CFG.Server.Port,
		"Port to listen on ",
	)
	flag.StringVar(
		&CFG.Locs.Osis,
		"osis",
		CFG.Locs.Osis,
		"Locs to OS DISK images",
	)
	flag.StringVar(
		&CFG.Locs.Bzis,
		"bzis",
		CFG.Locs.Bzis,
		"Locs to BZI images",
	)
	flag.StringVar(
		&CFG.Locs.Ptemplates,
		"ptemplates",
		CFG.Locs.Ptemplates,
		"Locs to templates",
	)
	flag.StringVar(
		&CFG.Locs.Pconfigs,
		"pconfigs",
		CFG.Locs.Pconfigs,
		"Locs to pxe-configs",
	)
	flag.StringVar(
		&CFG.Locs.Templates,
		"templates",
		CFG.Locs.Templates,
		"Locs to templates",
	)

	flag.Parse()

	JSON, err := json.MarshalIndent(CFG, "", "  ")
	if err != nil {
		LOG.Fatal("err: %v, json.Marshal(%v), ", err, CFG)
		return
	}

	log.Printf("Config below\n%s\n", JSON)

	osis := []Osi{}

	osis_load(CFG.Locs.Osis, osis, 0x0)

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
		Addr: fmt.Sprintf("%s:%s", CFG.Server.Host, CFG.Server.Port),
	}

	go func() {
		// Graceful shutdown
		sigquit := make(chan os.Signal, 1)
		signal.Notify(sigquit, os.Interrupt, os.Kill)

		sig := <-sigquit
		LOG.Printf("caught sig: %+v", sig)
		LOG.Printf("Gracefully shutting down server...")

		if err := server.Shutdown(context.Background()); err != nil {
			LOG.Printf("Unable to shut down server: %v", err)
		} else {
			LOG.Println("Server stopped")
		}
	}()

	server.ListenAndServe()
}
