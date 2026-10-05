import openai
from agent_logger.logger_config import logger
from model_client.retry import retry_wait

class ModelClient:
    def __init__(self,client,model_name,retries=3):
        self.client=client
        self.model_name=model_name
        self.retries=retries

    def call_model(self,input_data,instructions,tools):
        for attempt in range(self.retries):
            try:
                return self.client.responses.create(
                    model=self.model_name,
                    input=input_data,
                    instructions=instructions,
                    tools=tools
                )

            except openai.APIConnectionError as e:
                logger.error(
                    f"模型连接异常 "
                    f"attempt={attempt+1}/{self.retries} "
                    f"error={e}"
                )

                if attempt==self.retries-1:
                    raise
                retry_wait(attempt)

            except openai.RateLimitError as e:
                logger.warning(
                    f"模型请求过于频繁 "
                    f"attempt={attempt+1}/{self.retries} "
                    f"error={e}"
                )

                if attempt==self.retries-1:
                    raise
                    
                retry_wait(attempt)

            except openai.APIStatusError as e:
                status=e.status_code

                logger.error(
                    f"模型接口错误 "
                    f"status={status} "
                    f"attempt={attempt+1}/{self.retries} "
                    f"error={e}"
                )

                if status in [400,401,402,403,404,422]:
                    raise

                if status>=500:
                    if attempt==self.retries-1:
                        raise
                    retry_wait(attempt)
                    continue

                raise