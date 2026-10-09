class ModelClient:
    def __init__(self,client,model_name):
        self.client=client
        self.model_name=model_name

    def call_once(self, input_data, instructions, tools, timeout=60):
        """Runtime owns retries and counts every actual SDK request."""
        client = self.client.with_options(max_retries=0, timeout=timeout)
        return client.responses.create(
            model=self.model_name,
            input=input_data,
            instructions=instructions,
            tools=tools,
        )
