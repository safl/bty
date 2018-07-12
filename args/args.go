package args

import (
	"encoding/json"
	"flag"
	"log"
	. "github.com/safl/bty/conf"
)

func Parse() (Conf, error) {
	var cfg = Conf {}

	// Setup default config here
	cfg.Server.Host = "localhost"
	cfg.Server.Port = 80

	cfg.Locs.Osis = "/srv/osis"
	cfg.Locs.Bzis = "/srv/tftp/bzi"

	cfg.Locs.Pconfigs = "/srv/bty/pconfigs"
	cfg.Locs.Ptemplates = "/srv/bty/ptemplates"
	cfg.Locs.Templates = "/srv/bty/templates"

	cfg.Patterns.OsiExt = "/*.qcow2"
	cfg.Patterns.BziExt = "/*.bzImage"
	cfg.Patterns.PconfigExt = "/*"
	cfg.Patterns.PtemplateExt = "/pxe-*.cfg"

	// Overwrite default configuration with CLI arguments
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

	flag.Parse()

	// Initialize the configuration
	CFG_JSON, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		log.Fatal("err: %v, json.Marshal(%v), ", err, cfg)
		return cfg, err
	}
	log.Printf("Conf below\n%s\n", CFG_JSON)

	return cfg, nil
}

