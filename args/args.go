package args

import (
	"encoding/json"
	"flag"
	"log"
	"github.com/safl/bty/conf"
)

// Returns a Default configuration with Server and Locs attributes overwritten
// with CLI arguments
// NOTE: there is currently no mechanism for changing the Patterns configuration
// except for modifying the source
func Parse() (conf.Conf, error) {

	cfg := conf.Default()			// Load the default config

	flag.StringVar(
		&cfg.Server.Host,
		"host",
		cfg.Server.Host,
		"Hostname / Address to listen on",
	)
	flag.IntVar(
		&cfg.Server.Port,
		"port",
		cfg.Server.Port,
		"Port to listen on ",
	)
	flag.StringVar(
		&cfg.Locs.Osis,
		"osis",
		cfg.Locs.Osis,
		"Locs to OS DISK images",
	)
	flag.StringVar(
		&cfg.Locs.Bzis,
		"bzis",
		cfg.Locs.Bzis,
		"Locs to BZI images",
	)
	flag.StringVar(
		&cfg.Locs.Ptemplates,
		"ptemplates",
		cfg.Locs.Ptemplates,
		"Locs to templates",
	)
	flag.StringVar(
		&cfg.Locs.Pconfigs,
		"pconfigs",
		cfg.Locs.Pconfigs,
		"Locs to pxe-configs",
	)
	flag.StringVar(
		&cfg.Locs.Templates,
		"templates",
		cfg.Locs.Templates,
		"Locs to templates",
	)

	flag.Parse()				// Parse CLI args and overwrite

	// Initialize the configuration
	CFG_JSON, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		log.Fatal("err: %v, json.Marshal(%v), ", err, cfg)
		return cfg, err
	}
	log.Printf("Conf below\n%s\n", CFG_JSON)

	return cfg, nil
}

