import inspect

tool_registry={}

def tool(func):
    tool_registry[func.__name__]={
        "func":func,
        "schema":generate_schema(func)
    }
    return func


def generate_schema(func):
    type_map={
        str:"string",
        int:"integer",
        float:"number",
        bool:"boolean"
    }

    sig=inspect.signature(func) #获取函数的参数以及参数的数据类型
    doc=inspect.getdoc(func)or ""
    lines=doc.splitlines()
    properties={}
    required=[]
    description=""
    param_descriptions={}
    in_args=False

    for line in lines:
        stripped_line=line.strip()
        if not description and stripped_line and stripped_line!="Args:":
            description=stripped_line
        if stripped_line=="Args:":
            in_args=True
            continue
        if in_args and ":" in stripped_line:
            name,param_description=stripped_line.split(":",1)
            param_descriptions[name.strip()]=param_description.strip()


    for name,param in sig.parameters.items():
        json_type=type_map.get(param.annotation,"string")

        properties[name]={
            "type":json_type,
            "description":param_descriptions.get(name,"")
        }
        if param.default is inspect._empty:
            required.append(name)


    return {
        "type":"function",
        "name":func.__name__,
        "description":description,
        "parameters":{
            "type":"object",
            "properties":properties,
            "required":required
        }
    }