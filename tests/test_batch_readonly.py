from __future__ import annotations





import unittest


from unittest.mock import patch





from chatgpt_actuator import server








class BatchReadonlyToolTests(unittest.IsolatedAsyncioTestCase):


    async def test_readonly_tool_executes_allowed_actions(self):


        result = await server.batch_readonly(


            [


                {"tool": "system_info", "arguments": {}},


                {"tool": "filesystem_list_roots", "arguments": {}},


            ]


        )





        self.assertEqual(result["mode"], "parallel_readonly")


        self.assertEqual(result["requested"], 2)


        self.assertEqual(result["completed"], 2)


        self.assertEqual(result["succeeded"], 2)


        self.assertEqual(result["failed"], 0)





    async def test_readonly_tool_rejects_write_actions(self):


        with self.assertRaises(PermissionError):


            await server.batch_readonly(


                [


                    {


                        "tool": "filesystem_write_text",


                        "arguments": {"path": "X:/outside-root/should-not-exist.txt", "content": "blocked"},


                    }


                ]


            )





    async def test_readonly_mixed_batch_serializes_thread_sensitive_tools(self):


        with patch.dict(


            server._BATCH_TOOL_HANDLERS,


            {


                "system_info": lambda: {"kind": "system"},


                "window_active": lambda: {"kind": "window"},


            },


        ):


            result = await server.batch_readonly(


                [{"tool": "system_info"}, {"tool": "window_active"}]


            )





        self.assertEqual(result["mode"], "sequential")


        self.assertTrue(result["serialized"])


        self.assertEqual(result["succeeded"], 2)





    async def test_readonly_serial_batch_isolates_runtime_errors(self):


        def fail():


            raise RuntimeError("boom")





        with patch.dict(


            server._BATCH_TOOL_HANDLERS,


            {


                "window_active": fail,


                "window_list": lambda: {"windows": []},


            },


        ):


            result = await server.batch_readonly(


                [{"tool": "window_active"}, {"tool": "window_list"}]


            )





        self.assertEqual(result["completed"], 2)


        self.assertEqual(result["failed"], 1)


        self.assertEqual(result["succeeded"], 1)


        self.assertFalse(result["results"][0]["ok"])


        self.assertTrue(result["results"][1]["ok"])





if __name__ == "__main__":


    unittest.main()
