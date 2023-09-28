﻿using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading.Tasks;

namespace Flow
{
    public class Flow2
    {
        public class Input
        {
            public string Prompt { get; set; } = string.Empty;
        }

        public class Output
        {
            public string Out1 { get; set; } = string.Empty;
            public string Out2 { get; set; } = string.Empty;
        }


        public Flow2()
        {

        }

        public Output Execute(Input inputs)
        {
            var output = new Output();
            // execution code: start
            // sample node to call Tool in the same project
            var node1 = InternalTool.CallSimpleLLM(inputs.Prompt);

            // sample node to call Tool in standalone tool project
            var node2 = SampleTool.SimpleLLM.CallSimpleLLM(node1);

            output.Out1 = node1;
            output.Out2 = node2;
            // execution code: end
            return output;
        }
    }
}
