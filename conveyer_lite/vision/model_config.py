MODEL_GROUPS = {
    "GROUP_1_6": [
        {
            "path": "weights/1,6/uneven_heights_and_unfilled_windows_new1.pt",
            "conf": 0.4,
            "classes": ("flatness",),
        },
        {
            "path": "weights/1,6/window_sinks.pt",
            "conf": 0.15,
            "classes": ("objects",),
        },
    ],

    "GROUP_2_7": [
        {
            "path": "weights/2,7/long_omission_v.1.2.pt",
            "conf": 0.3,
            "classes": ("omission-long",),
        },
        {
            "path": "weights/2,7/contacts_long_v.1.pt",
            "conf": 0.3,
            "classes": ("contacts-long",),
        },
    ],

    "GROUP_3_5": [
        {
            "path": "weights/3,5/short_omission_v.1.2.pt",
            "conf": 0.4,
            "classes": ("omission-short",),
        },
        {
            "path": "weights/3,5/contacts_short.pt",
            "conf": 0.1,
            "classes": ("flatness_short",),
        },
    ],

    "GROUP_4": [
        {
            "path": "weights/4/contacts.pt",
            "conf": 0.4,
            "classes": ("contacts",),
        },
        {
            "path": "weights/4/platform_old.pt",
            "conf": 0.3,
            "classes": ("platform",),
        },
        {
            "path": "weights/4/sinks_v.1_m.pt",
            "conf": 0.3,
            "classes": ("shells",),
        },
        {
            "path": "weights/4/glass_v.1.pt",
            "conf": 0.3,
            "classes": ("glass",),
        },
        {
            "path": "weights/4/well_v.1.pt",
            "conf": 0.3,
            "classes": ("case", "case_central"),
        },
        {
            "path": "weights/4/pins.pt",
            "conf": 0.3,
            "classes": ("pin",),
        },
    ],
}

ROLE_TO_GROUP = {
    "INPUT_LEFT":  "GROUP_1_6",
    "INPUT_RIGHT": "GROUP_1_6",

    "SPIDER_LEFT":  "GROUP_2_7",
    "SPIDER_RIGHT": "GROUP_2_7",

    "SPIDER_IN":  "GROUP_3_5",
    "SPIDER_OUT": "GROUP_3_5",

    "TOP": "GROUP_4",
}
