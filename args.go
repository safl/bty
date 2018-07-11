package args

import (
	"encoding/json"
	"flag"
	"log"
)

// Representation of the BTY configuration
type Args struct {
	Server struct {
		Host		string	`json:"host"`
		Port		int	`json:"port"`
	} `json:"server"`

	Locs struct {
		Osis		string	`json:"osis"`
		Bzis		string	`json:"bzis"`
		Pconfigs	string	`json:"pconfigs"`
		Ptemplates	string	`json:"ptemplates"`
		Templates	string	`json:"templates"`
	} `json:"locs"`

	Patterns struct {
		OsiExt		string	`json:"osi_ext"`
		BziExt		string	`json:"bzi_ext"`
	} `json:"patterns"`
}

func initialize() {
	var args = Args {}

	// Setup default config here
	args.Server.Host = "localhost"
	args.Server.Port = 80

	args.Locs.Osis = "/srv/osis"
	args.Locs.Bzis = "/srv/tftp/bzi"

	args.Locs.Pconfigs = "/srv/bty/pconfigs"
	args.Locs.Ptemplates = "/srv/bty/ptemplates"
	args.Locs.Templates = "/srv/bty/templates"

	args.Patterns.OsiExt = "/*.qcow2"
	args.Patterns.BziExt = "/*.bzImage"

	// Overwrite default configuration with CLI arguments
	flag.StringVar(
		&args.Server.Host,
		"host",
		args.Server.Host,
		"Hostname / Address to listen on",
	)
	flag.IntVar(
		&args.Server.Port,
		"port",
		args.Server.Port,
		"Port to listen on ",
	)
	flag.StringVar(
		&args.Locs.Osis,
		"osis",
		args.Locs.Osis,
		"Locs to OS DISK images",
	)
	flag.StringVar(
		&args.Locs.Bzis,
		"bzis",
		args.Locs.Bzis,
		"Locs to BZI images",
	)
	flag.StringVar(
		&args.Locs.Ptemplates,
		"ptemplates",
		args.Locs.Ptemplates,
		"Locs to templates",
	)
	flag.StringVar(
		&args.Locs.Pconfigs,
		"pconfigs",
		args.Locs.Pconfigs,
		"Locs to pxe-configs",
	)
	flag.StringVar(
		&args.Locs.Templates,
		"templates",
		args.Locs.Templates,
		"Locs to templates",
	)

	flag.Parse()

	// Initialize the configuration
	ARGS_JSON, err := json.MarshalIndent(args, "", "  ")
	if err != nil {
		log.Fatal("err: %v, json.Marshal(%v), ", err, args)
		return
	}
	log.Printf("Args below\n%s\n", ARGS_JSON)

	return Args
}
