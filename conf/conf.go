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
		PconfigExt		string	`json:"pconfig_ext"`
		PtemplateExt		string	`json:"ptemplate_ext"`
	} `json:"patterns"`
}

