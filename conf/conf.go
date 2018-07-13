package conf

// Representation of the BTY configuration
type Conf struct {
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
		PconfigExt	string	`json:"pconfig_ext"`
		PtemplateExt	string	`json:"ptemplate_ext"`
		PtemplateLbl	string	`json:"ptemplate_lbl"`
	} `json:"patterns"`
}

// Default Configuration for BTY
func Default() Conf {
	cfg := Conf{}

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
	cfg.Patterns.PtemplateLbl = "^LABEL\\s+(.*)$"

	return cfg
}

