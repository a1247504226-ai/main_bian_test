class Assertion:
    ASSERT_COMPARE_IN = 'in'
    ASSERT_COMPARE_GREATER_THAN = 'greater_than'
    ASSERT_COMPARE_GREATER_EQUAL_THAN = 'greater_equal_than'
    ASSERT_COMPARE_LESS_THAN = 'less_than'
    ASSERT_COMPARE_LESS_EQUAL_THAN = 'less_equal_than'

    def assert_json(self, json_source, json_path, compare_value, compare_expr=''):
        """
        arg compare_expr only support below expression:
        'ASSERT_COMPARE_IN'  mean compare_value 'in' flow_result
        'ASSERT_COMPARE_GREATER_THAN' mean compare_value '>' flow_result
        'ASSERT_COMPARE_GREATER_EQUAL_THAN' mean compare_value '>=' flow_result
        'ASSERT_COMPARE_LESS_THAN' mean compare_value '<' flow_result
        'ASSERT_COMPARE_LESS_EQUAL_THAN' mean compare_value '<=' flow_result
        """
        print(json_path)
        paths = json_path.split('.')
        print(paths)
        target = json_source
        for key in paths:
            if type(target) is dict:
                assert key in target
                target = target[key]
            elif type(target) is list or type(target) is tuple:
                index = int(key)
                assert len(target) > index
                target = target[int(key)]
            else:
                raise Exception('Not supported type')
        print(target, compare_value)
        if len(compare_expr) == 0:
            assert compare_value == target
        elif compare_expr == self.ASSERT_COMPARE_IN:
            assert compare_value in target
        elif compare_expr == self.ASSERT_COMPARE_LESS_THAN:
            assert compare_value < target
        elif compare_expr == self.ASSERT_COMPARE_LESS_EQUAL_THAN:
            assert compare_value <= target
        elif compare_expr == self.ASSERT_COMPARE_GREATER_THAN:
            assert compare_value > target
        elif compare_expr == self.ASSERT_COMPARE_GREATER_EQUAL_THAN:
            assert compare_value >= target
        else:
            raise Exception('Not supported compare expression')


assertion = Assertion()
