import six


def parse_return_stack_on_code(codes):
    assert isinstance(codes, list), "return_stack_on_code must be a list"

    def parse(e):
        if isinstance(e, six.integer_types):
            code, subcodes = e, None
        elif isinstance(e, (list, tuple)):
            code, subcodes = e[:2]
            assert isinstance(code, six.integer_types), "return_stack_on_code/code must be int"
            if isinstance(subcodes, six.integer_types):
                subcodes = [subcodes]
            if isinstance(subcodes, (list, tuple)):
                assert all(isinstance(x, six.integer_types) for x in subcodes),\
                    "return_stack_on_code/subcode must be list(int)"
            else:
                raise ValueError("invalid return_stack_on_code/subcode(s): %s" % subcodes)
        else:
            raise ValueError("invalid return_stack_on_code/subcode(s): %s" % e)
        return code, subcodes

    return dict(map(parse, codes))
