"""Byte-exact populated child-entity banners captured from the live Chazy
Control Pro (TAV-CHAZY-CLTPRO FW 1.10.11) on 2026-05-20.

One instance of each queryable child type was created, queried, then deleted
(controller restored to empty afterward, verified). media / schedule /
config_preset are omitted: the device returns [ERROR]Unknown parameter for
their GET ... STATUS -- genuinely not queryable (create-time tracking only).

NOTE: firmware mislabels the GROUP and EVENT Info header as "Dante Preset
Info" (copy-paste bug); parsers must key off the body/columns, not that line.
"""

BANNER_GROUP_ALL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID 001 Name: TestGroup\n       Decoders:  1\n================================================================'

BANNER_GROUP_DETAIL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID 001 Name: TestGroup\n       Decoders:  1\n================================================================'

BANNER_EVENT_ALL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID 001 Name: TestEvent\n       Type: TCP\n       Address: \n       Port: 0\n       Interface: Control LAN\n       Data: \n       Request: \n       Resend Delay: 0\n       Resending: 0\n================================================================'

BANNER_EVENT_DETAIL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID 001 Name: TestEvent\n       Type: TCP\n       Address: \n       Port: 0\n       Interface: Control LAN\n       Data: \n       Request: \n       Resend Delay: 0\n       Resending: 0\n================================================================'

BANNER_WALL_ALL = '================================================================\n              TAV-CHAZY-CLTPRO Video Wall Info\n              FW Version: 1.10.11\n\nVW  Col    Row    CfgSel  Name\n01  02     02     01      NULL\n    OutID\n    --- --- --- ---\n    Cfg    Name\n    01     Preset 1\n           Class  From    Screen\n           A      001     H01V01 H02V01 H01V02 H02V02\n================================================================'

BANNER_WALL_DETAIL = '================================================================\n              TAV-CHAZY-CLTPRO Video Wall Info\n              FW Version: 1.10.11\n\nVW  Col    Row    CfgSel  Name\n01  02     02     01      NULL\n    OutID\n    --- --- --- ---\n    Cfg    Name\n    01     Preset 1\n           Class  From    Screen\n           A      001     H01V01 H02V01 H01V02 H02V02\n================================================================'

BANNER_DANTE_PRESET_ALL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID    Name\n001   TestDP\n    >>Dev                             Type  Chn  SubDev                          SubChn\n      Decoder-001                     video 01   Encoder-001                     01\n      Decoder-001                     audio 01   0                               00\n      Decoder-001                     audio 02   0                               00\n================================================================'

BANNER_DANTE_PRESET_DETAIL = '================================================================\n              TAV-CHAZY-CLTPRO Dante Preset Info\n              FW Version: 1.10.11\n\nID    Name\n001   TestDP\n    >>Dev                             Type  Chn  SubDev                          SubChn\n      Decoder-001                     video 01   Encoder-001                     01\n      Decoder-001                     audio 01   0                               00\n      Decoder-001                     audio 02   0                               00\n================================================================'
