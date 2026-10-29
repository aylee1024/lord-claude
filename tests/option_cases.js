export default [
  {
    "name": "plain",
    "input": "work",
    "expected": {
      "text": "work"
    }
  },
  {
    "name": "empty",
    "input": "  \r\n ",
    "expected": {
      "text": ""
    }
  },
  {
    "name": "readonly",
    "input": "@readonly\nwork",
    "expected": {
      "text": "work",
      "readonly": true
    }
  },
  {
    "name": "net",
    "input": "@net\nwork",
    "expected": {
      "text": "work",
      "net": true
    }
  },
  {
    "name": "nosearch",
    "input": "@nosearch\nwork",
    "expected": {
      "text": "work",
      "nosearch": true
    }
  },
  {
    "name": "nosandbox",
    "input": "@nosandbox\nwork",
    "expected": {
      "text": "work",
      "nosandbox": true
    }
  },
  {
    "name": "tests",
    "input": "@tests\nwork",
    "expected": {
      "text": "work",
      "tests": true
    }
  },
  {
    "name": "tools",
    "input": "@tools\nwork",
    "expected": {
      "text": "work",
      "limits": {
        "tools": true
      }
    }
  },
  {
    "name": "neutral options",
    "input": "@model fake-model\n@effort ultra\n@memory 3.5\n@hang 0\n@wall 9\n@nice 19\nwork",
    "expected": {
      "text": "work",
      "model": "fake-model",
      "effort": "ultra",
      "limits": {
        "memory_gb": 3.5,
        "hang_s": 0,
        "wall_s": 9,
        "nice": 19
      }
    }
  },
  {
    "name": "folders",
    "input": "@cwd /tmp/x/child\n@dir /tmp/x/one\n@dir /tmp/x/two\nwork",
    "expected": {
      "text": "work",
      "cwd": "/tmp/x/child",
      "dirs": [
        "/tmp/x/one",
        "/tmp/x/two"
      ]
    }
  },
  {
    "name": "folder spaces",
    "input": "@dir /tmp/x/two words  \nwork",
    "expected": {
      "text": "work",
      "dirs": [
        "/tmp/x/two words"
      ]
    }
  },
  {
    "name": "crlf whitespace",
    "input": " \t@net \r\nwork\r\n ",
    "expected": {
      "text": "work",
      "net": true
    }
  },
  {
    "name": "bom whitespace",
    "input": "\ufeff@net\nwork\ufeff",
    "expected": {
      "text": "work",
      "net": true
    }
  },
  {
    "name": "next line is text",
    "input": "\u0085@net\nwork",
    "expected": {
      "text": "\u0085@net\nwork"
    }
  },
  {
    "name": "zero width is text",
    "input": "\u200b@net\nwork",
    "expected": {
      "text": "\u200b@net\nwork"
    }
  },
  {
    "name": "line separator whitespace",
    "input": "@net\u2028\nwork",
    "expected": {
      "text": "work",
      "net": true
    }
  },
  {
    "name": "mention",
    "input": "@peer please review this\n@net",
    "expected": {
      "text": "@peer please review this\n@net"
    }
  },
  {
    "name": "uppercase is text",
    "input": "@NET\nwork",
    "expected": {
      "text": "@NET\nwork"
    }
  },
  {
    "name": "body stops options",
    "input": "work\n@nosandbox",
    "expected": {
      "text": "work\n@nosandbox"
    }
  },
  {
    "name": "blank stops options",
    "input": "@nosearch\n\n@nosandbox",
    "expected": {
      "text": "\n@nosandbox",
      "nosearch": true
    }
  },
  {
    "name": "repeat net",
    "input": "@net\n@net\nwork",
    "expected": {
      "text": "@net\nwork",
      "net": true,
      "error": "option line repeated: @net (only @dir may be given more than once)"
    }
  },
  {
    "name": "repeat model malformed",
    "input": "@model fake\n@model too many\nwork",
    "expected": {
      "text": "@model too many\nwork",
      "model": "fake",
      "error": "option line repeated: @model too many (only @dir may be given more than once)"
    }
  },
  {
    "name": "malformed @net yes",
    "input": "@net yes\nwork",
    "expected": {
      "text": "@net yes\nwork",
      "error": "option line not understood: @net yes"
    }
  },
  {
    "name": "malformed @readonly later",
    "input": "@readonly later\nwork",
    "expected": {
      "text": "@readonly later\nwork",
      "error": "option line not understood: @readonly later"
    }
  },
  {
    "name": "malformed @tests please run this",
    "input": "@tests please run this\nwork",
    "expected": {
      "text": "@tests please run this\nwork",
      "error": "option line not understood: @tests please run this"
    }
  },
  {
    "name": "malformed @tools yes",
    "input": "@tools yes\nwork",
    "expected": {
      "text": "@tools yes\nwork",
      "error": "option line not understood: @tools yes"
    }
  },
  {
    "name": "malformed @model",
    "input": "@model\nwork",
    "expected": {
      "text": "@model\nwork",
      "error": "option line not understood: @model"
    }
  },
  {
    "name": "malformed @model two words",
    "input": "@model two words\nwork",
    "expected": {
      "text": "@model two words\nwork",
      "error": "option line not understood: @model two words"
    }
  },
  {
    "name": "malformed @effort large",
    "input": "@effort large\nwork",
    "expected": {
      "text": "@effort large\nwork",
      "error": "option line not understood: @effort large"
    }
  },
  {
    "name": "malformed @memory -1",
    "input": "@memory -1\nwork",
    "expected": {
      "text": "@memory -1\nwork",
      "error": "option line not understood: @memory -1"
    }
  },
  {
    "name": "malformed @memory \u0661",
    "input": "@memory \u0661\nwork",
    "expected": {
      "text": "@memory \u0661\nwork",
      "error": "option line not understood: @memory \u0661"
    }
  },
  {
    "name": "malformed @nice 1.5",
    "input": "@nice 1.5\nwork",
    "expected": {
      "text": "@nice 1.5\nwork",
      "error": "option line not understood: @nice 1.5"
    }
  },
  {
    "name": "malformed @cwd",
    "input": "@cwd\nwork",
    "expected": {
      "text": "@cwd\nwork",
      "error": "option line not understood: @cwd"
    }
  },
  {
    "name": "malformed @dir",
    "input": "@dir\nwork",
    "expected": {
      "text": "@dir\nwork",
      "error": "option line not understood: @dir"
    }
  },
  {
    "name": "malformed @net-flag",
    "input": "@net-flag\nwork",
    "expected": {
      "text": "@net-flag\nwork",
      "error": "option line not understood: @net-flag"
    }
  },
  {
    "name": "unknown @efort max",
    "input": "@efort max\nwork",
    "expected": {
      "text": "@efort max\nwork",
      "error": "unknown option line: @efort max (options: @model, @effort, @cwd, @dir, @readonly, @net, @nosearch, @nosandbox, @tests, @tools, @memory, @hang, @wall, @nice)"
    }
  },
  {
    "name": "unknown @unknown",
    "input": "@unknown\nwork",
    "expected": {
      "text": "@unknown\nwork",
      "error": "unknown option line: @unknown (options: @model, @effort, @cwd, @dir, @readonly, @net, @nosearch, @nosandbox, @tests, @tools, @memory, @hang, @wall, @nice)"
    }
  },
  {
    "name": "coordinator envelope",
    "input": "The coordinator sent a message while you were working:\n@net\nwork\n\nAddress this before completing your current task.",
    "expected": {
      "text": "work",
      "net": true
    }
  },
  {
    "name": "workflow envelope",
    "input": "[Workflow harness \u2014 computed task]\nThe computed task text follows:\n\n  @nosearch\n  work",
    "expected": {
      "text": "work",
      "nosearch": true
    }
  },
  {
    "name": "notification text",
    "input": "<task-notification><task-id>probe</task-id><status>done</status><result>result</result></task-notification>\n@net",
    "expected": {
      "text": "Agent probe finished (done): result\n@net"
    }
  }
  ,{
    "name": "unicode word boundary",
    "input": "@net\u00e9\nwork",
    "expected": {"text": "@net\u00e9\nwork", "error": "option line not understood: @net\u00e9"}
  },{
    "name": "underscore is text",
    "input": "@net_flag\nwork",
    "expected": {"text": "@net_flag\nwork"}
  },{
    "name": "non whitespace model",
    "input": "@model \u0085\nwork",
    "expected": {"text": "work", "model": "\u0085"}
  },{
    "name": "workflow carriage return indentation",
    "input": "[Workflow harness \u2014 computed task]\nThe computed task text follows:\n  @cwd /tmp/x/one\r  two\n  work",
    "expected": {"text": "work", "cwd": "/tmp/x/one\rtwo"}
  },{
    "name": "unmatched notification opener",
    "input": "before <task-notification>tail",
    "expected": {"text": "before <task-notification>tail"}
  },{
    "name": "repeated unmatched notification openers",
    "input": "<task-notification><task-notification><task-notification>",
    "expected": {"text": "<task-notification><task-notification><task-notification>"}
  },{
    "name": "nested notification openers",
    "input": "<task-notification>outer<task-notification><task-id>probe</task-id><status>done</status><result>inner</result></task-notification>tail</task-notification>",
    "expected": {"text": "Agent probe finished (done): innertail</task-notification>"}
  },{
    "name": "unmatched notification field openers",
    "input": "<task-notification><result><result><status>done</status></task-notification>",
    "expected": {"text": "Agent  finished (done):"}
  }
]
